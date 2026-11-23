"""Offline IP → provider (ASN) / country / region / city, for DevVersion reports.

Databases: DB-IP «IP to ASN Lite» and «IP to City Lite» (.mmdb, CC BY 4.0 — attribution
«IP Geolocation by DB-IP», https://db-ip.com, is shown in the admin section). They live in
``WEB__GEOIP_DIR`` (a volume shared by web and worker); the worker's hourly task downloads a
missing or month-old file (at most one attempt per 6 hours, 10 minutes for the whole run, HTTPS
only and only from db-ip.com hosts — redirects included), gunzips it next to the live one,
checks its type (ASN / City) and a probe lookup, then swaps it in atomically. The web process
notices the new file by mtime and reads it memory-mapped.
No file / unreadable file / private address → every field is None; an upload never fails
because of this. Nothing here logs or keeps the address it was asked about.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import os
import time
import zlib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import maxminddb

from src.core.logging import get_logger

if TYPE_CHECKING:
    from src.infrastructure.di import AppContainer

log = get_logger(__name__)

ASN_FILE = "dbip-asn-lite.mmdb"
CITY_FILE = "dbip-city-lite.mmdb"
_SOURCES = (("dbip-asn-lite", ASN_FILE, "asn"), ("dbip-city-lite", CITY_FILE, "city"))
_URL = "https://download.db-ip.com/free/{name}-{month}.mmdb.gz"
MAX_AGE = dt.timedelta(days=32)  # DB-IP publishes a new Lite build every month
_ATTEMPT_KEY = "geoip:attempt"
_ATTEMPT_EVERY = 6 * 3600
_MAX_GZ_BYTES = 200 * 1024 * 1024
_MAX_MMDB_BYTES = 600 * 1024 * 1024
_RECHECK = 60.0  # seconds between mtime checks of an open database
_TOTAL_TIMEOUT = 600.0  # the whole refresh (both files)
_MAX_REDIRECTS = 3
_INFLATE_STEP = 4 * 1024 * 1024  # most output one decompress() call may produce
_PROBE_IP = "1.1.1.1"


def _open(path: Path) -> Any:
    """Memory-mapped reader: the C extension (MODE_MMAP_EXT) when it's installed, else the
    pure-Python MODE_MMAP. Never MODE_MEMORY — the City file is ~130 MB."""
    try:
        return maxminddb.open_database(str(path), maxminddb.MODE_MMAP_EXT)
    except ValueError:  # extension not available on this platform
        return maxminddb.open_database(str(path), maxminddb.MODE_MMAP)


@dataclass(frozen=True, slots=True)
class GeoInfo:
    asn: int | None = None
    as_org: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None


class _Db:
    """One .mmdb file, reopened when it is replaced on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.reader: Any = None
        self.mtime = 0.0
        self.checked = -_RECHECK

    def get(self, ip: str) -> Any:
        now = time.monotonic()
        if now - self.checked >= _RECHECK:
            self.checked = now
            self._reopen_if_changed()
        if self.reader is None:
            return None
        try:
            return self.reader.get(ip)
        except (ValueError, maxminddb.InvalidDatabaseError):
            return None

    def _reopen_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._close()
            return
        if self.reader is not None and mtime == self.mtime:
            return
        try:
            reader = _open(self.path)
        except (OSError, ValueError, maxminddb.InvalidDatabaseError):
            log.warning("geoip database unreadable", file=self.path.name)
            return
        self._close()
        self.reader, self.mtime = reader, mtime

    def _close(self) -> None:
        if self.reader is not None:
            with suppress(Exception):
                self.reader.close()
        self.reader = None


class GeoIp:
    def __init__(self, directory: str) -> None:
        self.asn = _Db(Path(directory) / ASN_FILE)
        self.city = _Db(Path(directory) / CITY_FILE)

    def lookup(self, ip: str) -> GeoInfo:
        addr = _public(ip)
        if addr is None:
            return GeoInfo()
        asn = _dict(self.asn.get(addr))
        city = _dict(self.city.get(addr))
        number = asn.get("autonomous_system_number")
        subdivisions = city.get("subdivisions")
        region = subdivisions[0] if isinstance(subdivisions, list) and subdivisions else None
        return GeoInfo(
            asn=number if type(number) is int else None,
            as_org=_text(asn.get("autonomous_system_organization"), 200),
            country=_text(_dict(city.get("country")).get("iso_code"), 2),
            region=_text(_local_name(region), 100),
            city=_text(_local_name(city.get("city")), 100),
        )


_instances: dict[str, GeoIp] = {}


def lookup(directory: str, ip: str) -> GeoInfo:
    """Provider and region of ``ip`` from the databases in ``directory`` (None-filled when
    they are missing). Readers are kept open per directory."""
    geo = _instances.get(directory)
    if geo is None:
        geo = _instances[directory] = GeoIp(directory)
    try:
        return geo.lookup(ip)
    except Exception:  # never let a broken database fail an upload
        return GeoInfo()


def status(directory: str) -> dict[str, Any]:
    """For the admin: which databases are present and when they were updated."""
    out: dict[str, Any] = {}
    for key, name in (("asn", ASN_FILE), ("city", CITY_FILE)):
        try:
            mtime = (Path(directory) / name).stat().st_mtime
        except OSError:
            out[key] = None
            continue
        out[key] = dt.datetime.fromtimestamp(mtime, dt.UTC).isoformat()
    return out


def _public(ip: str) -> str | None:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr) if addr.is_global else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _local_name(record: Any) -> Any:
    names = _dict(_dict(record).get("names"))
    return names.get("ru") or names.get("en")


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


# --- download (worker) ------------------------------------------------------------------------
def _due(path: Path, now: dt.datetime) -> bool:
    try:
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
    except OSError:
        return True
    return now - mtime > MAX_AGE


def _months(now: dt.datetime) -> tuple[str, str]:
    """This month and the previous one (early in a month the new build may not be out)."""
    prev = now.replace(day=1) - dt.timedelta(days=1)
    return now.strftime("%Y-%m"), prev.strftime("%Y-%m")


class _NotPublished(Exception):
    pass


def _allowed_url(url: str) -> bool:
    """Downloads (redirects included) only over HTTPS and only from db-ip.com hosts."""
    try:
        parts = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return False
    host = (parts.host or "").lower()
    return parts.scheme == "https" and (host == "db-ip.com" or host.endswith(".db-ip.com"))


async def _fetch(client: httpx.AsyncClient, url: str, dest: Path) -> None:
    for _ in range(_MAX_REDIRECTS + 1):
        if not _allowed_url(url):
            raise ValueError("geoip download: only https://*.db-ip.com is allowed")
        async with client.stream("GET", url) as res:
            if res.is_redirect:
                url = str(res.url.join(res.headers.get("location", "")))
                continue
            if res.status_code == 404:
                raise _NotPublished(url)
            res.raise_for_status()
            size = 0
            with dest.open("wb") as fh:
                async for chunk in res.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_GZ_BYTES:
                        raise ValueError("geoip download too large")
                    fh.write(chunk)
            return
    raise ValueError("geoip download: too many redirects")


def _check(path: Path, kind: str) -> None:
    """The file is the database it claims to be (ASN / City by its type) and answers a probe."""
    reader = _open(path)
    try:
        if kind not in str(reader.metadata().database_type).lower():
            raise ValueError(f"geoip: not a {kind} database")
        if not isinstance(reader.get(_PROBE_IP), dict):
            raise ValueError("geoip: probe lookup returned nothing")
    finally:
        reader.close()


def _inflate(inflater: Any, data: bytes, dst: Any, written: int) -> int:
    """Feed ``data`` to the inflater, never producing more than _INFLATE_STEP bytes per call
    (the rest waits in ``unconsumed_tail``), and stop past _MAX_MMDB_BYTES. Returns the new
    total written. Called with b"" at the end it drains what's left — a bounded ``flush``."""
    while True:
        out = inflater.decompress(data, _INFLATE_STEP)
        written += len(out)
        if written > _MAX_MMDB_BYTES:
            raise ValueError("geoip database too large")
        dst.write(out)
        data = inflater.unconsumed_tail
        if not data and len(out) < _INFLATE_STEP:
            return written


def _install(gz: Path, target: Path, kind: str) -> None:
    """gunzip (bounded) next to the live file, check it, swap it in atomically."""
    tmp = target.with_name(target.name + ".tmp")
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    written = 0
    try:
        with gz.open("rb") as src, tmp.open("wb") as dst:
            while chunk := src.read(1024 * 1024):
                written = _inflate(inflater, chunk, dst, written)
            _inflate(inflater, b"", dst, written)
        if not inflater.eof:
            raise ValueError("geoip archive truncated")
        _check(tmp, kind)
        os.replace(tmp, target)
    finally:
        with suppress(OSError):
            tmp.unlink()


async def refresh(directory: str, *, now: dt.datetime | None = None) -> dict[str, str]:
    """Download every missing / month-old database, all within one 10-minute deadline.
    Returns file → month (or what went wrong)."""
    now = now or dt.datetime.now(dt.UTC)
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    done: dict[str, str] = {}
    deadline = asyncio.get_running_loop().time() + _TOTAL_TIMEOUT
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=False) as client:
        for name, file, kind in _SOURCES:
            target = folder / file
            if not _due(target, now):
                continue
            gz = folder / f"{file}.gz.part"
            try:
                async with asyncio.timeout_at(deadline):
                    for month in _months(now):
                        try:
                            await _fetch(client, _URL.format(name=name, month=month), gz)
                        except _NotPublished:
                            continue
                        await asyncio.to_thread(_install, gz, target, kind)
                        done[file] = month
                        break
                    else:
                        done[file] = "not published"
            except TimeoutError:
                log.warning("geoip download timed out", file=file)
                done[file] = "timeout"
            except Exception as exc:  # network / disk / a broken file — retried later
                log.warning("geoip download failed", file=file, error=str(exc)[:200])
                done[file] = "error"
            finally:
                with suppress(OSError):
                    gz.unlink()
    if done:
        log.info("geoip databases refreshed", result=done)
    return done


async def refresh_if_due(container: AppContainer) -> dict[str, str]:
    """Worker task body: cheap when the files are fresh; otherwise one attempt per 6 hours
    (a host that can't reach db-ip.com must not download in a loop)."""
    settings = container.settings.web
    if not settings.geoip_download:
        return {}
    now = dt.datetime.now(dt.UTC)
    folder = Path(settings.geoip_dir)
    if not any(_due(folder / file, now) for _, file, _ in _SOURCES):
        return {}
    try:
        if not await container.redis.set(_ATTEMPT_KEY, "1", nx=True, ex=_ATTEMPT_EVERY):
            return {}
    except Exception:
        return {}
    return await refresh(settings.geoip_dir, now=now)
