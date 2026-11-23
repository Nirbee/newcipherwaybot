"""DevVersion — opt-in diagnostics from the Windows app: report checks, the summary digest and
the admin summaries. Pure functions (no I/O); the routes and tasks wire them up.

Contract (``/api/app/v1/telemetry``, see ``src/web/routes/app_telemetry.py``)::

    { schema: 1, consent_version: "1", install_id: "<uuid>", app_version, os: {...},
      mode: "tun"|"proxy", period: {from, to}, system: {...},
      apps: [...], probes: [...], nodes: [...], events: [...], game_tests: [...] }

The sections are stored as sent (JSONB), so only their TYPES and the overall shape (depth,
counts, string sizes, no NUL / lone surrogates / non-finite numbers — Postgres JSONB refuses
those) are checked. The summaries read a few known fields of the list items; anything else is
kept but not summarised (aliases in brackets are accepted too):

* ``nodes[]``: ``node`` [tile, name, server, tag], ``protocol`` [proto, transport],
  ``latency_ms`` [delay_ms, rtt_ms, ping_ms], ``loss_pct`` [loss, packet_loss],
  ``ok`` (bool), ``failures`` [fails, errors] (int);
* ``probes[]``: ``target`` [domain, host, url], ``route`` [via] = ``direct``/``vpn``, ``ok``,
  ``latency_ms``, ``loss_pct``, ``failures``; optional ``node`` / ``protocol`` (then it also
  counts toward provider x node) and ``app``;
* ``apps[]``: ``name`` [app, process, exe], ``direct_failures`` [direct_errors], ``domains[]``:
  ``domain`` [host, name] + ``direct_failures``;
* ``game_tests[]``: ``game`` [title, name, target], ``route``, ``latency_ms``, ``loss_pct``,
  ``jitter_ms`` [jitter], ``ok``, ``failures``.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import statistics
import urllib.parse
import uuid
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

SCHEMA = 1
CONSENT_VERSION = "1"
# Consent texts a report may have been collected under. A new text → a new version here, and
# apps that haven't shown it yet get 422 ``consent_version`` (the user must agree again).
ACCEPTED_CONSENT = frozenset({"1"})
MAX_REPORT_BYTES = 512 * 1024  # on the wire, gzip or not
MAX_JSON_BYTES = 512 * 1024  # after gunzip — a zip bomb is cut here
RETENTION_DAYS = 90
# Disk guards: counts per subscription and per IP, bytes (after gunzip) per account and for the
# whole table per day, a hard row cap per subscription, and an alert past a table size.
UPLOADS_PER_HOUR = 6  # per subscription
UPLOADS_PER_DAY = 60
UPLOADS_PER_IP_DAY = 100
BYTES_PER_USER_DAY = 20 * 1024 * 1024
BYTES_TOTAL_DAY = 1024 * 1024 * 1024
MAX_REPORTS_PER_SUBSCRIPTION = 500
TABLE_ALERT_BYTES = 3 * 1024 * 1024 * 1024
INTERVAL_DEFAULT = 3600
INTERVAL_MIN = 600
INTERVAL_MAX = 86400
FLAGS_MAX_KEYS = 50
FLAGS_MAX_STR = 64
_FLAG_KEY_RE = re.compile(r"[a-z0-9_]{1,32}")
ALLOWED_MAX = 1000  # subscriptions in the allow-list

OBJECT_SECTIONS = ("os", "period", "system")
LIST_SECTIONS = ("apps", "probes", "nodes", "events", "game_tests")
MODES = ("tun", "proxy")

# Shape caps: the payload lands in JSONB as is, so it must stay a sane document.
MAX_TOP_KEYS = 32
MAX_DEPTH = 10  # nested objects/lists, the report itself = 1
MAX_VALUES = 200_000  # every value in the tree
MAX_LIST_ITEMS = 20_000
MAX_OBJECT_KEYS = 1_000
MAX_KEY_LEN = 128
MAX_STR_LEN = 32_768
_MAX_INT = 2**63

_APP_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")

# Digest caps (per report) — what the summaries read instead of the payload.
_DIGEST_LINKS = 50
_DIGEST_DIRECT = 100
_DIGEST_GAMES = 100
_NAME_LEN = 64
# Value clamps, so one report can't drag a median or a total.
_MAX_FAILURES = 1000  # per item
_MAX_MS = 60_000  # latency / jitter


class ReportError(ValueError):
    """A rejected upload; ``code`` is the short reason the app gets back (``detail``)."""

    def __init__(self, code: str, status: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class Report:
    schema: int
    consent_version: str
    install_id: str
    app_version: str
    payload: dict[str, Any]
    size: int  # JSON bytes after gunzip


# --- settings ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DevVersionConfig:
    enabled: bool
    upload_interval_s: int
    flags: dict[str, Any]
    allowed: frozenset[int] = frozenset()  # empty → every non-trial subscription

    def refusal(self, subscription_id: int, *, trial: bool) -> str | None:
        """Why THIS subscription may not send (the 403 detail), or None."""
        if trial:
            return "trial_not_allowed"
        if self.allowed and subscription_id not in self.allowed:
            return "not_allowed"
        return None

    def public(self, reason: str | None = None) -> dict[str, Any]:
        """``GET /telemetry/config``; ``reason`` — the subscription's own refusal, if any."""
        if not self.enabled:
            reason = "disabled"
        return {
            "enabled": reason is None,
            "reason": reason,
            "consent_version": CONSENT_VERSION,
            "upload_interval_s": self.upload_interval_s,
            "max_report_bytes": MAX_REPORT_BYTES,
            "flags": self.flags if reason is None else {},
        }


def config_from(snap: Mapping[str, Any]) -> DevVersionConfig:
    """Effective DevVersion settings from a bot-config snapshot (tolerant: a bad stored value
    falls back to the default instead of breaking the endpoint)."""
    return DevVersionConfig(
        enabled=bool(snap.get("DEVVERSION_TELEMETRY_ENABLED", True)),
        upload_interval_s=clamp_interval(snap.get("DEVVERSION_UPLOAD_INTERVAL")),
        flags=parse_flags(snap.get("DEVVERSION_FLAGS")),
        allowed=parse_allowed(snap.get("DEVVERSION_ALLOWED_SUBSCRIPTIONS")),
    )


_ID_SPLIT = re.compile(r"[\s,;]+")


def parse_allowed(raw: Any) -> frozenset[int]:
    """«12, 345 678» → {12, 345, 678}; junk tokens are skipped (tolerant read)."""
    tokens = _ID_SPLIT.split(str(raw or ""))
    return frozenset(int(t) for t in tokens if t.isdigit() and 0 < int(t) < 2**31)


def clean_allowed(raw: Any) -> str:
    """Admin input (text or a list of ids) → «1,2,3» (sorted, unique), else ValueError."""
    items = raw if isinstance(raw, list) else _ID_SPLIT.split(str(raw or ""))
    ids: set[int] = set()
    for item in items:
        token = str(item).strip()
        if not token:
            continue
        if not token.isdigit() or not 0 < int(token) < 2**31:
            raise ValueError(
                f"DEVVERSION_ALLOWED_SUBSCRIPTIONS: «{token[:20]}» — не номер подписки"
            )
        ids.add(int(token))
    if len(ids) > ALLOWED_MAX:
        raise ValueError(f"DEVVERSION_ALLOWED_SUBSCRIPTIONS: не больше {ALLOWED_MAX} подписок")
    return ",".join(str(i) for i in sorted(ids))


def clamp_interval(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return INTERVAL_DEFAULT
    return min(INTERVAL_MAX, max(INTERVAL_MIN, n))


def check_interval(value: Any) -> int:
    """Admin input: an int within bounds, else ValueError with a message for the admin."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = -1
    if not INTERVAL_MIN <= n <= INTERVAL_MAX:
        raise ValueError(f"DEVVERSION_UPLOAD_INTERVAL: от {INTERVAL_MIN} до {INTERVAL_MAX} секунд")
    return n


def _flag_ok(key: Any, value: Any) -> bool:
    if not isinstance(key, str) or not _FLAG_KEY_RE.fullmatch(key):
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) < 2**53
    return isinstance(value, str) and len(value) <= FLAGS_MAX_STR


def parse_flags(raw: Any) -> dict[str, Any]:
    """Stored flags for the app: only well-formed entries (tolerant read)."""
    if not isinstance(raw, dict):
        try:
            raw = json.loads(str(raw or "{}"))
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return dict([(k, v) for k, v in raw.items() if _flag_ok(k, v)][:FLAGS_MAX_KEYS])


def clean_flags(raw: Any) -> str:
    """Admin input (JSON text or an object) → canonical JSON text of a FLAT object: keys
    ``[a-z0-9_]{1,32}``, values bool / int / a string of up to 64 chars, at most 50 keys.
    Else ValueError with a message for the admin."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError as exc:
            raise ValueError("DEVVERSION_FLAGS: это не JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError('DEVVERSION_FLAGS: нужен JSON-объект, например {"new_probe": true}')
    if len(raw) > FLAGS_MAX_KEYS:
        raise ValueError(f"DEVVERSION_FLAGS: не больше {FLAGS_MAX_KEYS} флагов")
    for key, value in raw.items():
        if not _flag_ok(key, value):
            raise ValueError(
                f"DEVVERSION_FLAGS: «{str(key)[:40]}» — ключ из a-z, 0-9, _ (до 32 символов), "
                f"значение — true/false, число или строка до {FLAGS_MAX_STR} символов"
            )
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def settings_guard(changes: dict[str, Any]) -> None:
    """Generic settings PATCH: the same checks as the DevVersion screen (normalised in place;
    ValueError with a message for the admin)."""
    if "DEVVERSION_UPLOAD_INTERVAL" in changes:
        changes["DEVVERSION_UPLOAD_INTERVAL"] = check_interval(
            changes["DEVVERSION_UPLOAD_INTERVAL"]
        )
    if "DEVVERSION_FLAGS" in changes:
        changes["DEVVERSION_FLAGS"] = clean_flags(changes["DEVVERSION_FLAGS"])
    if "DEVVERSION_ALLOWED_SUBSCRIPTIONS" in changes:
        changes["DEVVERSION_ALLOWED_SUBSCRIPTIONS"] = clean_allowed(
            changes["DEVVERSION_ALLOWED_SUBSCRIPTIONS"]
        )


# --- upload --------------------------------------------------------------------------------
def decode_body(raw: bytes, encoding: str) -> bytes:
    """The JSON bytes of an upload: ``identity`` as is, ``gzip`` inflated with a hard cap on
    the OUTPUT (never more than MAX_JSON_BYTES + 1 is produced, whatever the ratio)."""
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        if len(raw) > MAX_JSON_BYTES:
            raise ReportError("report_too_large", 413)
        return raw
    if encoding != "gzip":
        raise ReportError("unsupported_encoding", 415)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(raw, MAX_JSON_BYTES + 1)
    except zlib.error as exc:
        raise ReportError("bad_gzip", 400) from exc
    if len(out) > MAX_JSON_BYTES or inflater.unconsumed_tail:
        raise ReportError("report_too_large", 413)
    if not inflater.eof or inflater.unused_data:
        raise ReportError("bad_gzip", 400)
    return out


def _no_constants(name: str) -> Any:
    raise ValueError(f"not a JSON number: {name}")


def parse_report(body: bytes) -> Report:
    """Decoded JSON bytes → a checked report (ReportError on anything off-contract)."""
    try:
        data = json.loads(body, parse_constant=_no_constants)
    except (ValueError, RecursionError) as exc:
        raise ReportError("invalid_json", 400) from exc
    if not isinstance(data, dict):
        raise ReportError("invalid_report")
    if len(data) > MAX_TOP_KEYS:
        raise ReportError("too_many_sections")
    schema = data.get("schema")
    if type(schema) is not int or schema != SCHEMA:
        raise ReportError("unsupported_schema")
    consent = data.get("consent_version")
    if type(consent) is int:
        consent = str(consent)
    if not isinstance(consent, str) or consent not in ACCEPTED_CONSENT:
        raise ReportError("consent_version")
    install_id = data.get("install_id")
    try:
        install_id = str(uuid.UUID(str(install_id))) if isinstance(install_id, str) else None
    except ValueError:
        install_id = None
    if install_id is None:
        raise ReportError("install_id")
    app_version = data.get("app_version")
    if not isinstance(app_version, str) or not _APP_VERSION_RE.fullmatch(app_version):
        raise ReportError("app_version")
    if data.get("mode") is not None and data["mode"] not in MODES:
        raise ReportError("mode")
    for key in OBJECT_SECTIONS:
        if data.get(key) is not None and not isinstance(data[key], dict):
            raise ReportError(f"{key}_not_object")
    for key in LIST_SECTIONS:
        if data.get(key) is not None and not isinstance(data[key], list):
            raise ReportError(f"{key}_not_list")
    _check_shape(data)
    return Report(
        schema=schema,
        consent_version=consent,
        install_id=install_id,
        app_version=app_version,
        payload=data,
        size=len(body),
    )


def _bad_text(value: str) -> bool:
    if "\x00" in value:
        return True  # JSONB has no \u0000
    if value.isascii():
        return False
    try:
        value.encode("utf-8")  # lone surrogates (\ud800) can't be stored either
    except UnicodeEncodeError:
        return True
    return False


def _check_shape(root: dict[str, Any]) -> None:
    """Iterative walk (no recursion limit to trip): depth, sizes and storable scalars."""
    seen = 0
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        seen += 1
        if seen > MAX_VALUES:
            raise ReportError("too_many_values")
        if isinstance(value, dict):
            if depth > MAX_DEPTH:
                raise ReportError("too_deep")
            if len(value) > MAX_OBJECT_KEYS:
                raise ReportError("too_many_keys")
            for key, item in value.items():
                if len(key) > MAX_KEY_LEN or _bad_text(key):
                    raise ReportError("bad_key")
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            if depth > MAX_DEPTH:
                raise ReportError("too_deep")
            if len(value) > MAX_LIST_ITEMS:
                raise ReportError("list_too_long")
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            if len(value) > MAX_STR_LEN:
                raise ReportError("string_too_long")
            if _bad_text(value):
                raise ReportError("bad_string")
        elif isinstance(value, float):
            if not math.isfinite(value):  # 1e400 parses as inf
                raise ReportError("bad_number")
        elif isinstance(value, int) and not isinstance(value, bool) and abs(value) >= _MAX_INT:
            raise ReportError("bad_number")


def prepare_upload(raw: bytes, encoding: str) -> tuple[Report, dict[str, Any]]:
    """gunzip → checks → digest: CPU work on up to 512 KB, run in a thread by the route."""
    report = parse_report(decode_body(raw, encoding))
    return report, build_digest(report)


# --- digest (built once at upload) --------------------------------------------------------
def _first(item: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


def _num(value: Any, low: float, high: float) -> float | None:
    """A finite number clamped into [low, high], else None."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return round(float(min(high, max(low, value))), 2)


def _ms(value: Any) -> float | None:
    """Latency / jitter: 0..60000 ms; a negative time is junk, not zero."""
    if isinstance(value, int | float) and not isinstance(value, bool) and value < 0:
        return None
    return _num(value, 0, _MAX_MS)


def _pct(value: Any) -> float | None:
    return _num(value, 0, 100)


def _name(value: Any) -> str | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:_NAME_LEN] or None


def _host(value: Any) -> str | None:
    """A domain to group by: ``https://www.youtube.com/x`` → ``www.youtube.com``."""
    name = _name(value)
    if name is None:
        return None
    if "://" in name:
        with contextlib.suppress(ValueError):  # «https://[broken» — keep the text as is
            name = urllib.parse.urlsplit(name).hostname or name
    return name.lower()


def _route(item: Mapping[str, Any]) -> str | None:
    value = str(_first(item, ("route", "via")) or "").lower()
    if value in ("direct", "bypass"):
        return "direct"
    if value in ("vpn", "proxy", "tunnel"):
        return "vpn"
    return None


def _failures(item: Mapping[str, Any]) -> int:
    """An explicit count wins (0..1000); otherwise ``ok: false`` (or an error text) is one."""
    for key in ("failures", "fails", "errors"):
        value = item.get(key)
        if type(value) is int:
            return min(_MAX_FAILURES, max(0, value))
    if item.get("ok") is False or isinstance(item.get("error"), str):
        return 1
    return 0


def _count(value: Any) -> int:
    return min(_MAX_FAILURES, value) if type(value) is int and value > 0 else 0


_LATENCY = ("latency_ms", "delay_ms", "rtt_ms", "ping_ms")
_LOSS = ("loss_pct", "loss", "packet_loss")
_NODE = ("node", "tile", "name", "server", "tag")
_PROTO = ("protocol", "proto", "transport")


def _items(section: Any) -> list[Mapping[str, Any]]:
    return [i for i in section if isinstance(i, dict)] if isinstance(section, list) else []


def build_digest(report: Report) -> dict[str, Any]:
    """The few numbers the admin summaries need, taken from one report (bounded)."""
    payload = report.payload
    links: list[list[Any]] = []
    for item in _items(payload.get("nodes")):
        links.append(
            [
                _name(_first(item, _NODE)),
                _name(_first(item, _PROTO)),
                _ms(_first(item, _LATENCY)),
                _pct(_first(item, _LOSS)),
                _failures(item),
            ]
        )
    direct: dict[tuple[str, str], int] = {}
    for item in _items(payload.get("probes")):
        node, proto = _name(item.get("node")), _name(item.get("protocol"))
        if node or proto:
            links.append(
                [
                    node,
                    proto,
                    _ms(_first(item, _LATENCY)),
                    _pct(_first(item, _LOSS)),
                    _failures(item),
                ]
            )
        fails = _failures(item)
        if _route(item) == "direct" and fails:
            target = _host(_first(item, ("target", "domain", "host", "url")))
            if target:
                direct[("domain", target)] = direct.get(("domain", target), 0) + fails
            app = _name(item.get("app"))
            if app:
                direct[("app", app)] = direct.get(("app", app), 0) + fails
    for item in _items(payload.get("apps")):
        app = _name(_first(item, ("name", "app", "process", "exe")))
        fails = _count(_first(item, ("direct_failures", "direct_errors")))
        if app and fails:
            direct[("app", app)] = direct.get(("app", app), 0) + fails
        for dom in _items(item.get("domains")):
            host = _host(_first(dom, ("domain", "host", "name")))
            n = _count(_first(dom, ("direct_failures", "direct_errors")))
            if host and n:
                direct[("domain", host)] = direct.get(("domain", host), 0) + n
    games: list[list[Any]] = []
    for item in _items(payload.get("game_tests")):
        games.append(
            [
                _name(_first(item, ("game", "title", "name", "target"))),
                _route(item),
                _ms(_first(item, _LATENCY)),
                _pct(_first(item, _LOSS)),
                _ms(_first(item, ("jitter_ms", "jitter"))),
                _failures(item),
            ]
        )
    top_direct = sorted(direct.items(), key=lambda kv: -kv[1])[:_DIGEST_DIRECT]
    return {
        "v": 1,
        "size": report.size,
        "mode": payload.get("mode"),
        "counts": {
            key: len(payload[key]) for key in LIST_SECTIONS if isinstance(payload.get(key), list)
        },
        "links": links[:_DIGEST_LINKS],
        "direct": [[kind, name, min(_MAX_FAILURES, n)] for (kind, name), n in top_direct],
        "games": games[:_DIGEST_GAMES],
    }


# --- admin summaries ------------------------------------------------------------------------
# One subscription can't outweigh the rest: per group it adds at most this many samples (to
# medians and failures), at most this many failures to a direct-failure total, and it can open
# at most this many node / game groups (so it can't flood the tables with made-up names).
SUB_SAMPLES_CAP = 100
SUB_FAILURES_CAP = 1000
SUB_GROUPS_CAP = 50
SUMMARY_CELLS = 200_000  # digest rows a summary walks before it stops (``truncated``)


@dataclass(frozen=True, slots=True)
class DigestRow:
    report_id: int
    subscription_id: int
    asn: int | None
    as_org: str | None
    digest: Mapping[str, Any]


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


@dataclass
class _Acc:
    reports: set[int] = field(default_factory=set)
    subs: dict[int, int] = field(default_factory=dict)  # subscription → samples it sent
    samples: int = 0
    failures: int = 0
    latency: list[float] = field(default_factory=list)
    loss: list[float] = field(default_factory=list)
    jitter: list[float] = field(default_factory=list)

    def add(self, row: DigestRow, failures: Any, *values: Any) -> None:
        sent = self.subs.get(row.subscription_id, 0)
        self.reports.add(row.report_id)
        self.subs[row.subscription_id] = sent + 1
        if sent >= SUB_SAMPLES_CAP:
            return
        self.samples += 1
        self.failures += min(_MAX_FAILURES, max(0, failures)) if type(failures) is int else 0
        for bucket, value in zip((self.latency, self.loss, self.jitter), values, strict=False):
            if isinstance(value, int | float) and not isinstance(value, bool):
                bucket.append(float(value))

    def stats(self) -> dict[str, Any]:
        return {
            "subscriptions": len(self.subs),
            "reports": len(self.reports),
            "samples": self.samples,
            "failures": self.failures,
            "latency_median": _median(self.latency),
            "loss_median": _median(self.loss),
        }


@dataclass
class _Direct:
    subs: dict[int, int] = field(default_factory=dict)  # subscription → failures (capped)
    providers: set[int | None] = field(default_factory=set)
    reports: set[int] = field(default_factory=set)

    def add(self, row: DigestRow, n: int) -> None:
        self.reports.add(row.report_id)
        self.providers.add(row.asn)
        had = self.subs.get(row.subscription_id, 0)
        self.subs[row.subscription_id] = min(SUB_FAILURES_CAP, had + max(0, n))


def _row(cells: Any, size: int) -> list[Any] | None:
    return list(cells) if isinstance(cells, list) and len(cells) == size else None


def _section(digest: Mapping[str, Any], key: str) -> list[Any]:
    value = digest.get(key)
    return value if isinstance(value, list) else []


def digest_cells(digest: Mapping[str, Any]) -> int:
    """How many rows a summary walks for this digest (the cell ceiling counts these)."""
    return sum(len(_section(digest, key)) for key in ("links", "direct", "games"))


def summarize(
    rows: Iterable[DigestRow], *, top: int = 100, max_cells: int = SUMMARY_CELLS
) -> dict[str, Any]:
    """Provider x node/protocol, direct failures by app/domain, game tests — from digests.

    Rows are ranked by how many DIFFERENT subscriptions (and, for direct failures, providers)
    saw them, not by raw sums, and cut AFTER that ranking — one noisy client can't top a list
    or push real rows out; rows seen by one subscription only carry ``single_source``. Stops
    after ``max_cells`` digest rows (``truncated``). CPU-bound: callers run it in a thread."""
    orgs: dict[int | None, str | None] = {}
    links: dict[tuple[int | None, str | None, str | None], _Acc] = {}
    direct: dict[tuple[str, str], _Direct] = {}
    games: dict[tuple[int | None, str | None, str | None], _Acc] = {}
    opened: dict[int, set[tuple[Any, ...]]] = {}  # subscription → node/game groups it is in

    def group(
        table: dict[Any, _Acc], tag: str, key: tuple[Any, ...], row: DigestRow
    ) -> _Acc | None:
        mine = opened.setdefault(row.subscription_id, set())
        tagged = (tag, *key)
        if tagged not in mine:
            if len(mine) >= SUB_GROUPS_CAP:
                return None
            mine.add(tagged)
        return table.setdefault(key, _Acc())

    count = cells_seen = 0
    truncated = False
    for row in rows:
        link_cells = _section(row.digest, "links")
        direct_cells = _section(row.digest, "direct")
        game_cells = _section(row.digest, "games")
        size = digest_cells(row.digest)
        if cells_seen + size > max_cells:
            truncated = True
            break
        cells_seen += size
        count += 1
        if row.as_org:
            orgs[row.asn] = row.as_org
        orgs.setdefault(row.asn, None)
        for raw in link_cells:
            cells = _row(raw, 5)
            if cells is not None:
                node, proto, lat, loss, fails = cells
                acc = group(links, "link", (row.asn, node, proto), row)
                if acc is not None:
                    acc.add(row, fails, lat, loss)
        for raw in direct_cells:
            cells = _row(raw, 3)
            if cells is not None and type(cells[2]) is int:
                direct.setdefault((str(cells[0]), str(cells[1])), _Direct()).add(row, cells[2])
        for raw in game_cells:
            cells = _row(raw, 6)
            if cells is not None:
                game, route, lat, loss, jitter, fails = cells
                acc = group(games, "game", (row.asn, game, route), row)
                if acc is not None:
                    acc.add(row, fails, lat, loss, jitter)

    def provider(asn: int | None) -> dict[str, Any]:
        return {"asn": asn, "as_org": orgs.get(asn)}

    link_rows = [
        {**provider(asn), "node": node, "protocol": proto, **acc.stats()}
        for (asn, node, proto), acc in links.items()
    ]
    link_rows.sort(key=lambda r: (-r["subscriptions"], -r["samples"], r["asn"] or 0))
    game_rows = [
        {
            **provider(asn),
            "game": game,
            "route": route,
            **acc.stats(),
            "jitter_median": _median(acc.jitter),
        }
        for (asn, game, route), acc in games.items()
    ]
    game_rows.sort(key=lambda r: (-r["subscriptions"], -r["samples"], str(r["game"])))
    direct_rows: list[dict[str, Any]] = [
        {
            "kind": kind,
            "name": name,
            "subscriptions": len(acc.subs),
            "providers": len(acc.providers),
            "reports": len(acc.reports),
            "failures": sum(acc.subs.values()),
        }
        for (kind, name), acc in direct.items()
    ]
    direct_rows.sort(
        key=lambda r: (-r["subscriptions"], -r["providers"], -r["failures"], r["name"])
    )
    for table_rows in (link_rows, game_rows, direct_rows):
        for r in table_rows:
            r["single_source"] = r["subscriptions"] == 1
    return {
        "reports": count,
        "truncated": truncated,
        "links": link_rows[: top * 5],
        "direct_failures": direct_rows[:top],
        "games": game_rows[: top * 5],
    }
