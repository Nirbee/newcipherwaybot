"""«Клиенты» — the admin's customer list: bot data merged with ONE panel snapshot.

The panel is asked for everything at once (every user via ``list_users``, every HWID device
via ``list_hwid_devices``) and the answer is cached for a minute in the web process, so the
list never calls Remnawave per row. Bot users are read in one SQL query; the two are matched
by the same keys ``panel_import`` uses (panel id, uuid, shortUuid, link code).

Filtering, search, sorting and paging run in memory over the merged rows: ~110 customers
today, 5 000 would still be a few milliseconds — and the panel-side filters (online, never
connected, devices over the limit…) could not be pushed to SQL anyway. When the panel is
unreachable the list still works on bot data; panel columns stay empty and the answer says
why (``panel.error``).

Also here: :func:`expire_orphaned` — the clean-up of subscriptions still ACTIVE/TRIAL in the
bot long after their expiry whose panel user no longer exists (DB only, never the panel).
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from src.application.services.panel_guard import is_service_username
from src.application.services.panel_import import _panel_keys, _sub_keys, _url_code
from src.core.constants import UNLIMITED_EXPIRE_DAYS
from src.core.enums import Role, SubscriptionStatus, UserStatus
from src.core.exceptions import RemnawaveAuthError, RemnawaveError, RemnawaveTransientError
from src.core.logging import get_logger
from src.infrastructure.database.models.server_node import ServerNode
from src.infrastructure.database.models.server_squad import ServerSquad
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

if TYPE_CHECKING:
    from src.application.common.panel import RemnawaveClient
    from src.application.dto.panel import PanelUser
    from src.infrastructure.database.uow import UnitOfWork

log = get_logger(__name__)

ONLINE_WINDOW = dt.timedelta(minutes=2)
# A far-future expiry is how «бессрочно» is stored (now + UNLIMITED_EXPIRE_DAYS, or 2099).
_FOREVER_AFTER = dt.timedelta(days=UNLIMITED_EXPIRE_DAYS // 2)
_LIVE = frozenset({"active", "trial", "limited", "grace"})
TOP_TRAFFIC = 20


# --- panel snapshot -------------------------------------------------------------------------


@dataclass(slots=True)
class PanelSnapshot:
    fetched_at: dt.datetime | None
    users: list[PanelUser] = field(default_factory=list)
    by_key: dict[str, PanelUser] = field(default_factory=dict)
    devices: dict[str, int] | None = None  # "id:<n>"/"uuid:<u>" -> devices; None = unknown
    error: str | None = None  # the panel didn't answer; ``users`` may be the last good copy
    devices_error: str | None = None
    # Whether this panel reports connection times at all: without them «never connected» would
    # wrongly match everyone, so the chip stays empty instead.
    has_connection_facts: bool = False

    def find(self, keys: Iterable[str]) -> PanelUser | None:
        for key in keys:
            user = self.by_key.get(key)
            if user is not None:
                return user
        return None

    def device_count(self, user: PanelUser) -> int | None:
        if self.devices is None:
            return None
        for key in (f"id:{user.panel_id}", f"uuid:{user.uuid}"):
            if key in self.devices:
                return self.devices[key]
        return 0


def error_code(exc: BaseException) -> str:
    """What the admin page is told about a failed panel call: a code, never the exception
    text (it may carry panel addresses or answers). The full text goes to the log."""
    if isinstance(exc, RemnawaveAuthError):
        return "auth"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, RemnawaveTransientError):
        return "unreachable"
    if isinstance(exc, RemnawaveError):
        return "rejected"
    return "error"


def _index(users: list[PanelUser]) -> dict[str, PanelUser]:
    out: dict[str, PanelUser] = {}
    for user in users:
        for key in _panel_keys(user):
            out.setdefault(key, user)
    return out


class PanelSnapshotService:
    """All panel users + device counts, cached ``ttl`` seconds (a failure ``failure_ttl``)."""

    def __init__(
        self,
        client: RemnawaveClient,
        *,
        ttl: float = 60.0,
        failure_ttl: float = 20.0,
        timeout: float = 25.0,
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._timeout = timeout
        self._snap: PanelSnapshot | None = None
        self._good: PanelSnapshot | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self, *, force: bool) -> bool:
        if self._snap is None:
            return False
        age = time.monotonic() - self._at
        if force:
            return age < 15.0  # a burst of «обновить» clicks is one panel round-trip
        return age < (self._ttl if self._snap.error is None else self._failure_ttl)

    async def get(self, *, force: bool = False) -> PanelSnapshot:
        if self._fresh(force=force):
            assert self._snap is not None
            return self._snap
        async with self._lock:
            if not self._fresh(force=force):
                self._snap = await self._fetch()
                self._at = time.monotonic()
            assert self._snap is not None
            return self._snap

    def invalidate(self) -> None:
        """After an admin action — the next read asks the panel again."""
        self._at = float("-inf")

    async def _fetch(self) -> PanelSnapshot:
        now = dt.datetime.now(dt.UTC)
        try:
            users = await asyncio.wait_for(self._client.list_users(), timeout=self._timeout)
        except Exception as exc:
            reason = error_code(exc)
            log.warning("clients: panel snapshot failed", code=reason, error=str(exc)[:300])
            good = self._good
            return PanelSnapshot(
                fetched_at=good.fetched_at if good else None,
                users=good.users if good else [],
                by_key=good.by_key if good else {},
                devices=good.devices if good else None,
                error=reason,
                has_connection_facts=good.has_connection_facts if good else False,
            )
        snap = PanelSnapshot(
            fetched_at=now,
            users=users,
            by_key=_index(users),
            has_connection_facts=any(u.online_at or u.first_connected_at for u in users),
        )
        try:
            devices = await asyncio.wait_for(
                self._client.list_hwid_devices(), timeout=self._timeout
            )
        except Exception as exc:
            snap.devices_error = error_code(exc)
            log.warning("clients: hwid devices listing failed", error=str(exc)[:300])
        else:
            counts: dict[str, int] = {}
            for device in devices:
                if device.user_key:
                    counts[device.user_key] = counts.get(device.user_key, 0) + 1
            snap.devices = counts
        self._good = snap
        return snap


# --- statuses ---------------------------------------------------------------------------------


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=dt.UTC)


def effective_status(
    *,
    user_status: UserStatus,
    sub_status: SubscriptionStatus | None,
    expire_at: dt.datetime | None,
    grace_until: dt.datetime | None,
    now: dt.datetime,
    panel_status: str | None = None,
) -> str:
    """What the admin should see: the stored status, except that a «working» subscription
    whose expiry has passed shows as expired — the stored one goes stale when nothing (no
    panel webhook, its panel user deleted) tells the bot. A grace window is its own state."""
    if user_status is UserStatus.BLOCKED:
        return "blocked"
    if sub_status is None or sub_status is SubscriptionStatus.PENDING:
        return "none"
    grace = _aware(grace_until)
    if grace is not None and grace > now and sub_status.is_usable:
        return "grace"
    if sub_status in (SubscriptionStatus.EXPIRED, SubscriptionStatus.DELETED):
        return "expired"
    if sub_status is SubscriptionStatus.DISABLED:
        return "disabled"
    end = _aware(expire_at)
    if end is not None and end <= now:
        return "expired"
    if sub_status is SubscriptionStatus.LIMITED or (panel_status or "").upper() == "LIMITED":
        return "limited"
    if sub_status is SubscriptionStatus.TRIAL:
        return "trial"
    return "active"


def _source(snapshot: dict[str, Any] | None) -> str:
    raw = str((snapshot or {}).get("source") or "")
    return raw or "bot"


# --- rows ---------------------------------------------------------------------------------------


_COLUMNS = (
    User.id.label("id"),
    User.telegram_id.label("telegram_id"),
    User.username.label("username"),
    User.first_name.label("first_name"),
    User.last_name.label("last_name"),
    User.email.label("email"),
    User.status.label("user_status"),
    User.role.label("role"),
    User.auth_type.label("auth_type"),
    User.created_at.label("created_at"),
    Subscription.id.label("sub_id"),
    Subscription.short_id.label("short_id"),
    Subscription.status.label("sub_status"),
    Subscription.is_trial.label("is_trial"),
    Subscription.expire_at.label("expire_at"),
    Subscription.grace_until.label("grace_until"),
    Subscription.traffic_used_bytes.label("traffic_used_bytes"),
    Subscription.traffic_limit_bytes.label("traffic_limit_bytes"),
    Subscription.device_limit.label("device_limit"),
    Subscription.plan_snapshot.label("plan_snapshot"),
    Subscription.internal_squads.label("internal_squads"),
    Subscription.remnawave_id.label("remnawave_id"),
    Subscription.remnawave_uuid.label("remnawave_uuid"),
    Subscription.subscription_url.label("subscription_url"),
    Subscription.panel_username.label("panel_username"),
)


def _record_keys(r: Any) -> set[str]:
    """``panel_import._sub_keys`` for a column row instead of a ``Subscription``."""
    keys: set[str] = set()
    if r["remnawave_id"] is not None:
        keys.add(f"id:{r['remnawave_id']}")
    if r["remnawave_uuid"] is not None:
        keys.add(f"uuid:{r['remnawave_uuid']}")
    if r["short_id"]:
        keys.add(f"code:{r['short_id']}")
    code = _url_code(r["subscription_url"])
    if code:
        keys.add(f"code:{code}")
    return keys


@dataclass(slots=True)
class ClientList:
    rows: list[dict[str, Any]]
    panel_names: dict[int, str]  # sub id -> panel username that differs from the stored one


async def load_rows(
    uow: UnitOfWork,
    snap: PanelSnapshot,
    *,
    service_patterns: tuple[str, ...],
    now: dt.datetime,
) -> ClientList:
    """Every bot user (staff included, the synthetic SYSTEM actor not) as a list row."""
    stmt = (
        select(*_COLUMNS)
        .outerjoin(Subscription, Subscription.id == User.current_subscription_id)
        .where(User.role != Role.SYSTEM)
    )
    records = (await uow.session.execute(stmt)).mappings().all()
    squads = {
        str(s.squad_uuid).lower(): s.display_name
        for s in (await uow.session.scalars(select(ServerSquad))).all()
    }
    nodes = {
        str(n.node_uuid).lower(): (n.name, n.country_code)
        for n in (await uow.session.scalars(select(ServerNode))).all()
    }
    rows: list[dict[str, Any]] = []
    names: dict[int, str] = {}
    for r in records:
        panel = snap.find(_record_keys(r)) if r["sub_id"] is not None else None
        rows.append(_row(r, panel, snap, squads, nodes, service_patterns, now))
        if panel is not None and panel.username and panel.username != r["panel_username"]:
            names[int(r["sub_id"])] = panel.username[:64]
    return ClientList(rows=rows, panel_names=names)


def _row(
    r: Any,
    panel: PanelUser | None,
    snap: PanelSnapshot,
    squads: dict[str, str],
    nodes: dict[str, tuple[str, str | None]],
    patterns: tuple[str, ...],
    now: dt.datetime,
) -> dict[str, Any]:
    has_sub = r["sub_id"] is not None
    # The bot's expiry is what was paid for (the panel's may be a grace window's short one);
    # the panel's only fills in for a subscription the bot has no date for.
    expire_at = (_aware(r["expire_at"]) if has_sub else None) or (
        _aware(panel.expire_at) if panel is not None else None
    )
    grace_until = _aware(r["grace_until"]) if has_sub else None
    status = effective_status(
        user_status=r["user_status"],
        sub_status=r["sub_status"] if has_sub else None,
        expire_at=expire_at,
        grace_until=grace_until,
        now=now,
        panel_status=panel.status if panel is not None else None,
    )
    panel_name = (panel.username if panel is not None else None) or r["panel_username"]
    used = panel.traffic_used_bytes if panel is not None else int(r["traffic_used_bytes"] or 0)
    limit = int(r["traffic_limit_bytes"] or 0) if has_sub else 0
    if panel is not None:
        limit = panel.traffic_limit_bytes
    device_limit = r["device_limit"] if has_sub else None
    if panel is not None and panel.device_limit is not None:
        device_limit = panel.device_limit or None  # 0 on the panel = unlimited
    node: dict[str, Any] | None = None
    if panel is not None and (panel.last_node_uuid or panel.last_node_name):
        known = nodes.get((panel.last_node_uuid or "").lower())
        node = {
            "name": panel.last_node_name or (known[0] if known else None),
            "country": panel.last_node_country or (known[1] if known else None),
        }
        if not node["name"]:
            node = None
    squad_ids = (
        list(panel.internal_squads) if panel is not None else list(r["internal_squads"] or [])
    )
    online_at = _aware(panel.online_at) if panel is not None else None
    first_at = _aware(panel.first_connected_at) if panel is not None else None
    created_at = _aware(r["created_at"])
    name = " ".join(filter(None, [r["first_name"], r["last_name"]])) or None
    snapshot = r["plan_snapshot"] if has_sub else None
    return {
        "id": r["id"],
        "telegram_id": r["telegram_id"],
        "username": r["username"],
        "name": name,
        "email": r["email"],
        "auth_type": r["auth_type"].value if r["auth_type"] is not None else None,
        "role": r["role"].name,
        "status": status,
        "is_service": is_service_username(panel_name, patterns),
        "subscription_id": r["sub_id"],
        "short_id": r["short_id"],
        "plan_name": (snapshot or {}).get("name") if has_sub else None,
        "source": _source(snapshot) if has_sub else None,
        "expire_at": expire_at.isoformat() if expire_at else None,
        "expire_forever": bool(expire_at and expire_at - now > _FOREVER_AFTER),
        "grace_until": grace_until.isoformat() if grace_until and status == "grace" else None,
        "traffic_used_bytes": used,
        "traffic_limit_bytes": limit,
        "device_limit": device_limit,
        "devices": snap.device_count(panel) if panel is not None else None,
        "online_at": online_at.isoformat() if online_at else None,
        "first_connected_at": first_at.isoformat() if first_at else None,
        "never_connected": (
            panel is not None
            and snap.has_connection_facts
            and first_at is None
            and online_at is None
        ),
        "node": node,
        "squads": [squads.get(str(s).lower(), str(s)[:8]) for s in squad_ids],
        "tag": panel.tag if panel is not None else None,
        "panel_username": panel_name,
        "panel_status": panel.status if panel is not None else None,
        "in_panel": panel is not None,
        "created_at": created_at.isoformat() if created_at else None,
    }


# --- chips, search, sort ----------------------------------------------------------------------


def _dt(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def _days_left(row: dict[str, Any], now: dt.datetime) -> float | None:
    end = _dt(row["expire_at"])
    return (end - now).total_seconds() / 86400 if end else None


def _chips(now: dt.datetime) -> dict[str, Callable[[dict[str, Any]], bool]]:
    def expiring(days: int) -> Callable[[dict[str, Any]], bool]:
        def check(row: dict[str, Any]) -> bool:
            left = _days_left(row, now)
            return row["status"] in _LIVE and left is not None and 0 < left <= days

        return check

    def online(row: dict[str, Any]) -> bool:
        at = _dt(row["online_at"])
        return at is not None and now - at <= ONLINE_WINDOW

    def expired_week(row: dict[str, Any]) -> bool:
        left = _days_left(row, now)
        return left is not None and -7 <= left <= 0

    def gone(row: dict[str, Any]) -> bool:
        at = _dt(row["online_at"])
        return at is not None and now - at > dt.timedelta(days=7)

    def near_limit(row: dict[str, Any]) -> bool:
        limit = row["traffic_limit_bytes"] or 0
        return limit > 0 and row["traffic_used_bytes"] >= 0.8 * limit

    def devices_over(row: dict[str, Any]) -> bool:
        return (
            row["devices"] is not None
            and bool(row["device_limit"])
            and row["devices"] > row["device_limit"]
        )

    def new_week(row: dict[str, Any]) -> bool:
        at = _dt(row["created_at"])
        return at is not None and now - at <= dt.timedelta(days=7)

    return {
        "all": lambda row: True,
        "active": lambda row: row["status"] in _LIVE,
        "online": online,
        "exp3": expiring(3),
        "exp7": expiring(7),
        "expired_week": expired_week,
        "never": lambda row: bool(row["never_connected"]),
        "gone": gone,
        "near_limit": near_limit,
        "devices_over": devices_over,
        "new_week": new_week,
        "limited": lambda row: row["status"] == "limited",
        "disabled": lambda row: row["status"] == "disabled",
        "expired": lambda row: row["status"] == "expired",
        "top_traffic": lambda row: row["traffic_used_bytes"] > 0,
        "no_sub": lambda row: row["status"] == "none",
        "trial": lambda row: row["status"] == "trial",
        "blocked": lambda row: row["status"] == "blocked",
    }


def counters(rows: list[dict[str, Any]], now: dt.datetime) -> dict[str, int]:
    """Count per chip; service accounts only under «service», never in the others."""
    chips = _chips(now)
    regular = [r for r in rows if not r["is_service"]]
    out = {name: sum(1 for r in regular if check(r)) for name, check in chips.items()}
    out["top_traffic"] = min(out["top_traffic"], TOP_TRAFFIC)
    out["service"] = len(rows) - len(regular)
    return out


def _haystack(row: dict[str, Any], *, private: bool = False) -> str:
    parts = [row["name"], row["username"], row["panel_username"], row["tag"], row["plan_name"]]
    if not private:
        # e-mail and the link code: not for read-only keys / demo — a search that answers
        # «found» would leak them one guess at a time
        parts += [row["email"], row["short_id"]]
    return " ".join(str(p).lower() for p in parts if p)


def matches(row: dict[str, Any], q: str, *, private: bool = False) -> bool:
    """Name, mark, e-mail, @username, Telegram ID (or its start), panel name / short id
    (``sub_…``), bot id (``id75`` or the bare number)."""
    q = q.strip().lower()
    if not q:
        return True
    text = q.lstrip("@")
    hay = _haystack(row, private=private)
    if text in hay or text.removeprefix("sub_") in hay:
        return True
    digits = text.removeprefix("id").removeprefix("user_").removeprefix("tg:")
    if digits.isascii() and digits.isdigit() and len(digits) <= 18:
        tg = str(row["telegram_id"] or "")
        if tg == digits or (len(digits) >= 4 and tg.startswith(digits)):
            return True
        if not text.startswith(("user_", "tg:")) and row["id"] == int(digits):
            return True
    return False


_SORTS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "name": lambda r: (r["name"] or r["panel_username"] or r["username"] or "").lower() or None,
    "status": lambda r: r["status"],
    "expire": lambda r: r["expire_at"],
    "traffic": lambda r: r["traffic_used_bytes"],
    "online": lambda r: r["online_at"],
    "node": lambda r: (r["node"] or {}).get("name"),
    "devices": lambda r: r["devices"],
    "squads": lambda r: ", ".join(r["squads"]) or None,
    "tag": lambda r: r["tag"],
    "source": lambda r: r["source"],
    "created": lambda r: r["created_at"],
    "id": lambda r: r["id"],
}


def select_rows(
    rows: list[dict[str, Any]],
    *,
    chip: str,
    q: str,
    sort: str,
    desc: bool,
    now: dt.datetime,
    private: bool = False,
) -> list[dict[str, Any]]:
    """The chip's rows matching ``q``, sorted (empty values always last). ``private`` — a key
    or demo session: no search by e-mail / link code."""
    if chip == "service":
        picked = [r for r in rows if r["is_service"]]
    else:
        check = _chips(now).get(chip, _chips(now)["all"])
        picked = [r for r in rows if not r["is_service"] and check(r)]
    if chip == "top_traffic":
        picked.sort(key=lambda r: r["traffic_used_bytes"], reverse=True)
        picked = picked[:TOP_TRAFFIC]
    picked = [r for r in picked if matches(r, q, private=private)]
    if chip == "top_traffic" and sort == "id":
        return picked
    key = _SORTS.get(sort, _SORTS["id"])
    filled = [r for r in picked if key(r) is not None]
    empty = [r for r in picked if key(r) is None]
    filled.sort(key=key, reverse=desc)
    return filled + empty


_CSV_HEAD = (
    ("id", "ID в боте"),
    ("name", "Имя"),
    ("username", "Telegram @"),
    ("telegram_id", "Telegram ID"),
    ("email", "E-mail"),
    ("panel_username", "Имя в панели"),
    ("tag", "Метка"),
    ("status", "Статус"),
    ("plan_name", "Тариф"),
    ("expire_at", "Действует до"),
    ("traffic_used_bytes", "Трафик, байт"),
    ("traffic_limit_bytes", "Лимит трафика, байт"),
    ("devices", "Устройств"),
    ("device_limit", "Лимит устройств"),
    ("online_at", "Был онлайн"),
    ("node", "Нода"),
    ("squads", "Сквады"),
    ("source", "Источник"),
    ("created_at", "Регистрация"),
)


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    """Text a spreadsheet would run as a formula (``=HYPERLINK(…)`` as a customer's name)
    gets a leading apostrophe; numbers stay numbers."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def to_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([title for _, title in _CSV_HEAD])
    for row in rows:
        out: list[Any] = []
        for key, _ in _CSV_HEAD:
            value = row.get(key)
            if key == "node":
                value = (value or {}).get("name")
            elif key == "squads":
                value = ", ".join(value or [])
            out.append("" if value is None else _csv_cell(value))
        writer.writerow(out)
    return "\ufeff" + buf.getvalue()  # BOM: Excel opens the Cyrillic as UTF-8


# --- clean-up of stale statuses ---------------------------------------------------------------


_STALE = (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.LIMITED)
# Not the moment of expiry: a renewal or the grace sweep may be on its way.
_STALE_AFTER = dt.timedelta(days=1)
# More than this in one run looks like a panel/matching fault, not real deletions.
ORPHAN_LIMIT = 15


@dataclass(slots=True)
class OrphanReport:
    changed: list[int] = field(default_factory=list)
    candidates: int = 0
    skipped: int = 0  # the panel couldn't confirm «not found» — left as is
    over_limit: bool = False  # too many at once: nothing changed, admins alerted


async def expire_orphaned(
    uow: UnitOfWork,
    panel_users: list[PanelUser],
    now: dt.datetime,
    *,
    client: RemnawaveClient,
    limit: int = ORPHAN_LIMIT,
) -> OrphanReport:
    """Mark EXPIRED (in the bot's DB only) the subscriptions that still say ACTIVE/TRIAL/LIMITED
    a day after their expiry AND their panel user is gone — nothing will ever tell the
    bot otherwise (no panel user, no webhook), so the counters stayed inflated for months.

    Three locks against a wrong «gone»: an empty panel list is a hiccup (nothing is touched;
    an incomplete one already raised in ``list_users``); more than ``limit`` candidates at once
    is treated as a fault, not reality (nothing is changed, the caller alerts the admins); and
    every candidate is asked about once more with ``get_user_strict`` — only an explicit «no
    such user» counts, a panel error skips it. A grace window (``grace_until``) is left to the
    grace sweep. The panel is only read. Caller commits."""
    report = OrphanReport()
    if not panel_users:
        return report
    present: set[str] = set()
    for user in panel_users:
        present |= _panel_keys(user)
    stmt = select(Subscription).where(
        Subscription.status.in_(_STALE),
        Subscription.grace_until.is_(None),
        Subscription.expire_at.is_not(None),
        Subscription.expire_at < now - _STALE_AFTER,
    )
    candidates = [
        sub for sub in (await uow.session.scalars(stmt)).all() if not _sub_keys(sub) & present
    ]
    report.candidates = len(candidates)
    if len(candidates) > limit:
        report.over_limit = True
        return report
    for sub in candidates:
        ref = sub.panel_ref
        if ref is not None:
            try:
                if await client.get_user_strict(ref) is not None:
                    report.skipped += 1  # the list missed it — it is there after all
                    continue
            except Exception as exc:
                log.warning("expire_orphaned: lookup failed", sub=sub.id, error=str(exc))
                report.skipped += 1
                continue
        sub.status = SubscriptionStatus.EXPIRED
        report.changed.append(sub.id)
    if report.changed:
        await uow.session.flush()
    return report
