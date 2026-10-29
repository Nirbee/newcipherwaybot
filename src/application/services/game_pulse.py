"""Game pulse + agent event journal (router agent v7) — pure functions, no HTTP/DB/redis.

Game pulse: the agent times a round trip to a few game-region addresses, straight from the
router and through each VPN server's test port, so the admin sees which path is fastest for
games from this customer's ISP. Metric: two HEADs in one curl; the first opens the connection
(VPN tunnel, TCP, TLS) and is discarded, the second's ``time_starttransfer`` over that reused
connection is the path's round trip — comparable direct vs through a server. Privacy: only
the agent's OWN short connections to the fixed addresses listed here are timed — never
clients' traffic, conntrack, DNS or devices; only numbers (ms / failed) leave the router.

Event journal: when the agent applied a config (Xray restart), the watchdog switched the VPN
off/on, the agent self-updated or rolled back — game drops line up against these.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.core import config_registry as registry

MAX_TARGETS = 4
EVERY_RUNS = 2  # cron runs every 5 min -> a pulse every 10 min
TIMEOUT_S = 3

EVENTS_KEEP = 500
EVENTS_TTL_SECONDS = 30 * 86400
PULSE_TTL_SECONDS = 7 * 86400
# Agent sends at most 4 targets x 5 paths (direct + 4 test ports); anything beyond is dropped,
# so a token holder can't bloat redis or the card.
MAX_PATHS = 5
_MAX_RESULTS = MAX_TARGETS * MAX_PATHS
# Distinct paths kept per router: the agent's 5 plus one server it just moved away from. Rows
# of older paths are dropped, so a server change is measured at once.
MAX_DEVICE_PATHS = 6
_MAX_RUNS = 2100  # 7 days of a pulse every 5 min, should every_runs ever drop to 1
_MAX_MS = 60_000
LOSS_LIMIT = 0.2  # a path losing this share of measurements (24 h) can't be «best»

# Events that cut every connection through the router (Xray restarted or stopped).
RESTART_KINDS = frozenset({"config_applied", "rollback", "watchdog_stop"})

_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_KIND_RE = re.compile(r"[a-z_]{1,32}")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
_URL_PATH_RE = re.compile(r"[A-Za-z0-9._~/%-]*")
# Pulse path names: «direct» and the test-port tags (proxy-xxxxxxxx, cand-xxxxxxxx).
_PULSE_PATH_RE = re.compile(r"[a-z0-9-]{1,32}")
# Names a router would resolve inside the customer's own network.
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".keenetic.net")


class TargetError(ValueError):
    """A pulse target the agent couldn't safely dial."""


def _global_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def clean_target(item: Any) -> dict[str, str]:
    """Validated ``{id, title, url, games[, resolve_ip]}``. The URL must be a plain
    ``https://host[:443]/path`` to a public address (the agent splits its pairs on spaces and
    pins ``host:443`` for ``resolve_ip``); nothing in the customer's own network can be
    targeted. Raises TargetError with a Russian message otherwise."""
    if not isinstance(item, dict):
        raise TargetError("цель должна быть объектом")
    tid = str(item.get("id") or "").strip()
    if not _ID_RE.fullmatch(tid):
        raise TargetError(f"id «{tid}»: латиница в нижнем регистре, цифры и «-», до 32 символов")
    url = str(item.get("url") or "").strip(" ")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise TargetError(f"{tid}: в адресе пробелы или управляющие символы")
    if not url.startswith("https://") or len(url) > 200:
        raise TargetError(f"{tid}: адрес должен начинаться с https://")
    hostport, _, path = url.removeprefix("https://").partition("/")
    host, _, port = hostport.partition(":")
    if not _HOST_RE.fullmatch(host) or port not in ("", "443"):
        raise TargetError(f"{tid}: неверный адрес {url} (порт — только 443)")
    name = host.lower().rstrip(".")
    is_ip = all(part.isdigit() for part in name.split("."))
    if is_ip and not _global_ip(name):
        raise TargetError(f"{tid}: адрес {host} не публичный")
    if not is_ip and (
        "." not in name
        or name == "localhost"
        or name.endswith(_LOCAL_SUFFIXES)
        or name == "keenetic.net"
    ):
        raise TargetError(f"{tid}: {host} — имя локальной сети, нужен публичный адрес")
    if not _URL_PATH_RE.fullmatch(path):
        raise TargetError(f"{tid}: в пути адреса недопустимые символы")
    out = {
        "id": tid,
        "title": str(item.get("title") or "").strip()[:64] or tid,
        "url": url,
        "games": str(item.get("games") or "").strip()[:200],
    }
    resolve_ip = str(item.get("resolve_ip") or "").strip()
    if resolve_ip:
        try:
            ipaddress.IPv4Address(resolve_ip)
        except ValueError as exc:
            raise TargetError(f"{tid}: фиксированный IP должен быть IPv4-адресом") from exc
        if not _global_ip(resolve_ip):
            raise TargetError(f"{tid}: фиксированный IP {resolve_ip} не публичный")
        out["resolve_ip"] = resolve_ip
    return out


def clean_targets(items: Iterable[Any]) -> list[dict[str, str]]:
    """Strict: every item valid, ids unique, at most MAX_TARGETS (raises TargetError)."""
    out = [clean_target(i) for i in items]
    if len(out) > MAX_TARGETS:
        raise TargetError(f"не больше {MAX_TARGETS} целей — роутер слабый")
    ids = [t["id"] for t in out]
    if len(set(ids)) != len(ids):
        raise TargetError("id целей должны быть разными")
    return out


def default_targets() -> list[dict[str, str]]:
    return [dict(t) for t in registry.GAME_PULSE_DEFAULT_TARGETS]


def parse_targets(raw: Any) -> list[dict[str, str]]:
    """The GAME_PULSE_TARGETS setting as stored (JSON text, also editable raw on the generic
    settings screen). Empty or unreadable -> the defaults; bad entries are skipped."""
    if isinstance(raw, str):
        if not raw.strip():
            return default_targets()
        try:
            raw = json.loads(raw)
        except ValueError:
            return default_targets()
    if not isinstance(raw, list):
        return default_targets()
    out: list[dict[str, str]] = []
    for item in raw:
        try:
            target = clean_target(item)
        except TargetError:
            continue
        if all(t["id"] != target["id"] for t in out):
            out.append(target)
    return out[:MAX_TARGETS]


def agent_settings(enabled: bool, targets: list[dict[str, str]]) -> dict[str, Any]:
    """The /api/agent/pulse answer. A switched-off router gets no targets at all."""
    return {
        "enabled": enabled,
        "every_runs": EVERY_RUNS,
        "timeout_s": TIMEOUT_S,
        "targets": [
            {k: t[k] for k in ("id", "url", "resolve_ip") if t.get(k)}
            for t in (targets if enabled else [])
        ][:MAX_TARGETS],
    }


# --- events -------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    """An agent-sent number, or None for junk — incl. JSON Infinity/NaN and bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def merge_events(stored: list[Any], incoming: Any, now: float) -> list[dict[str, Any]]:
    """Stored journal + what the agent sent (it re-sends its last 20 every report): unique by
    ts+kind, last 30 days, at most EVENTS_KEEP, oldest first. Garbage entries are dropped; a
    router clock far in the future (no NTP yet) can't pin an event forever."""
    seen: dict[tuple[int, str], dict[str, Any]] = {}
    fresh = incoming[-50:] if isinstance(incoming, list) else []
    for e in [*stored, *fresh]:
        if not isinstance(e, dict):
            continue
        ts = _int(e.get("ts"))
        kind = str(e.get("kind") or "")
        if ts is None or not _KIND_RE.fullmatch(kind):
            continue
        if not now - EVENTS_TTL_SECONDS < ts <= now + 86400:
            continue
        seen.setdefault(
            (ts, kind), {"ts": ts, "kind": kind, "note": str(e.get("note") or "")[:120]}
        )
    return sorted(seen.values(), key=lambda e: (e["ts"], e["kind"]))[-EVENTS_KEEP:]


def restart_counts(events: list[dict[str, Any]], now: float) -> dict[str, int]:
    restarts = [e for e in events if e.get("kind") in RESTART_KINDS]
    return {
        "day": sum(1 for e in restarts if e["ts"] > now - 86400),
        "week": sum(1 for e in restarts if e["ts"] > now - 7 * 86400),
    }


# --- pulse --------------------------------------------------------------------------------


def append_pulse(
    stored: list[Any], incoming: Any, now: float, targets: set[str]
) -> list[dict[str, Any]]:
    """Adds one pulse report as ``{"ts", "r": [[target, path, ms|None], ...]}`` (compact: 7 days
    of them are kept). Stamped with the server's time — the windows below are server-side.
    Only the current ``targets``, well-formed path names, at most _MAX_RESULTS rows per report
    and MAX_DEVICE_PATHS distinct paths per router (the most recently reported) are kept."""
    runs = [
        r
        for r in stored
        if isinstance(r, dict) and isinstance(r.get("ts"), int | float)
        if r["ts"] > now - PULSE_TTL_SECONDS and isinstance(r.get("r"), list)
    ]
    results = incoming.get("results") if isinstance(incoming, dict) else None
    rows: list[list[Any]] = []
    for item in results if isinstance(results, list) else []:
        if len(rows) >= _MAX_RESULTS:
            break
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        path = item.get("path")
        if not isinstance(target, str) or target not in targets or not isinstance(path, str):
            continue
        if not _PULSE_PATH_RE.fullmatch(path):
            continue
        ms = _int(item.get("ms"))
        rows.append([target, path, ms if ms is not None and 0 <= ms <= _MAX_MS else None])
    if rows:
        runs.append({"ts": int(now), "r": rows})
    # Newest first: the paths the router reports now win over ones it no longer has.
    keep: list[str] = []
    for run in reversed(runs):
        for row in run["r"]:
            path = row[1] if isinstance(row, list) and len(row) == 3 else None
            if isinstance(path, str) and path not in keep and len(keep) < MAX_DEVICE_PATHS:
                keep.append(path)
    kept = set(keep)
    out = []
    for run in runs[-_MAX_RUNS:]:
        r = [row for row in run["r"] if isinstance(row, list) and len(row) == 3 and row[1] in kept]
        if r:
            out.append({"ts": run["ts"], "r": r})
    return out


@dataclass(frozen=True, slots=True)
class Cell:
    median_1h: int | None
    median_24h: int | None
    loss_1h: float | None
    loss_24h: float | None
    samples_1h: int
    samples_24h: int

    @property
    def median(self) -> int | None:
        """What the card shows and «best» ranks by: last hour, else last 24 h."""
        return self.median_1h if self.median_1h is not None else self.median_24h

    @property
    def usable(self) -> bool:
        return self.median is not None and self.loss_24h is not None and self.loss_24h < LOSS_LIMIT


def _median(values: list[int]) -> int | None:
    return round(statistics.median(values)) if values else None


def aggregate(runs: list[dict[str, Any]], now: float) -> dict[tuple[str, str], Cell]:
    """Per target x path: median round trip and share of failures over 1 h and 24 h."""
    hour: dict[tuple[str, str], list[int | None]] = {}
    day: dict[tuple[str, str], list[int | None]] = {}
    for run in runs:
        ts = run.get("ts") or 0
        if ts <= now - 86400:
            continue
        for row in run.get("r") or []:
            if not isinstance(row, list) or len(row) != 3:
                continue
            key = (str(row[0]), str(row[1]))
            day.setdefault(key, []).append(row[2])
            if ts > now - 3600:
                hour.setdefault(key, []).append(row[2])
    cells: dict[tuple[str, str], Cell] = {}
    for key, samples in day.items():
        h = hour.get(key, [])
        cells[key] = Cell(
            median_1h=_median([v for v in h if v is not None]),
            median_24h=_median([v for v in samples if v is not None]),
            loss_1h=(sum(1 for v in h if v is None) / len(h)) if h else None,
            loss_24h=sum(1 for v in samples if v is None) / len(samples),
            samples_1h=len(h),
            samples_24h=len(samples),
        )
    return cells


def best_paths(cells: dict[tuple[str, str], Cell]) -> dict[str, str]:
    """Per target: the path with the lowest median among those losing < LOSS_LIMIT."""
    best: dict[str, tuple[int, int, str]] = {}
    for (target, path), cell in cells.items():
        if not cell.usable or cell.median is None:
            continue
        # A tie goes to «direct»: a game that isn't slower without the VPN shouldn't use it.
        rank = (cell.median, 0 if path == "direct" else 1, path)
        current = best.get(target)
        if current is None or rank < current:
            best[target] = rank
    return {t: p for t, (_, _, p) in best.items()}


def _verdict(
    title: str, best: str | None, row: dict[str, Cell], path_title: dict[str, str]
) -> str | None:
    if not row:
        return None
    if best is None:
        return f"{title} — ни один путь не держит связь стабильно"
    best_ms = row[best].median or 0
    if best == "direct":
        vpn = [c.median for p, c in row.items() if p != "direct" and c.usable and c.median]
        if vpn:
            return f"{title} быстрее напрямую (на {min(vpn) - best_ms} мс)"
        return f"{title} — только напрямую"
    via = path_title.get(best, best)
    direct = row.get("direct")
    if direct is not None and direct.usable and direct.median is not None:
        return f"{title} — через «{via}» (на {direct.median - best_ms} мс быстрее, чем напрямую)"
    if direct is not None:
        return f"{title} — через «{via}» (напрямую связи нет)"
    return f"{title} — через «{via}»"


def pulse_card(
    runs: list[dict[str, Any]],
    targets: list[dict[str, str]],
    path_titles: dict[str, str],
    now: float,
) -> dict[str, Any]:
    """The «Игры и пинг» block: one row per current target, one column per path (direct
    first, at most MAX_DEVICE_PATHS)."""
    known = {t["id"]: t for t in targets}
    cells = {k: c for k, c in aggregate(runs, now).items() if k[0] in known}
    best = best_paths(cells)
    by_target: dict[str, dict[str, Cell]] = {}
    seen_paths: set[str] = set()
    for (t, p), c in cells.items():
        by_target.setdefault(t, {})[p] = c
        seen_paths.add(p)
    order: list[str] = ["direct"]
    listed = {"direct"}
    for run in reversed(runs):  # newest report first: the paths the router has now
        if run.get("ts", 0) <= now - 86400:
            break
        for row in run.get("r") or []:
            if isinstance(row, list) and len(row) == 3 and str(row[1]) not in listed:
                listed.add(str(row[1]))
                order.append(str(row[1]))
    paths = [p for p in order if p in seen_paths][:MAX_DEVICE_PATHS]
    titles = {"direct": "Напрямую", **path_titles}
    rows: list[dict[str, Any]] = []
    verdicts: list[str] = []
    for tid, meta in known.items():
        row = {p: c for p, c in by_target.get(tid, {}).items() if p in paths}
        verdict = _verdict(meta["title"], best.get(tid), row, titles)
        if verdict:
            verdicts.append(verdict)
        rows.append(
            {
                "target": tid,
                "title": meta["title"],
                "games": meta.get("games") or "",
                "best": best.get(tid),
                "cells": {
                    p: {
                        "median_1h": c.median_1h,
                        "median_24h": c.median_24h,
                        "loss_1h": c.loss_1h,
                        "loss_24h": c.loss_24h,
                        "samples_24h": c.samples_24h,
                    }
                    for p, c in row.items()
                },
            }
        )
    return {
        "paths": [{"id": p, "title": titles.get(p, p)} for p in paths],
        "rows": rows,
        "verdict": "; ".join(verdicts),
        "last_at": max((int(r.get("ts") or 0) for r in runs), default=0) or None,
    }
