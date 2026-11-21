"""LAN Play orchestration: config, the Headscale client, locks, device keys, views, cleanup.

Consistency between our DB (rooms) and the Headscale policy — one flow, all or nothing:
every room change runs under ONE Redis lock (``lan:policy-lock``); inside it the change is
flushed (not committed), the whole policy is rebuilt from that state and PUT, and only after
a successful PUT is the transaction committed. Headscale down / key rejected → the DB change
is rolled back, the DB policy is re-PUT best-effort (in case the PUT landed but the commit
didn't) and the caller answers 503. On top of that :func:`cleanup` PUTs the DB policy on every
tick (10 min), and no device key is handed out before the server's policy is confirmed to be
the DB one — Headscale without a stored policy allows everything, so LAN fails closed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import secrets
import time
import urllib.parse
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from src.application.services.bot_config import SECRET_MASK
from src.application.services.lan import (
    PREAUTH_KEY_TTL,
    build_policy,
    generate_code,
    is_lan_user,
    is_our_hostname,
    member_label,
    new_hostname,
    policy_json,
    public_device_name,
    same_policy,
    subscription_lan_active,
)
from src.core.enums import UserStatus
from src.core.exceptions import HeadscaleAuthError, HeadscaleError, HeadscalePolicyReadOnly
from src.core.logging import get_logger
from src.infrastructure.database.models.lan_room import LanRoom, LanRoomMember
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User
from src.infrastructure.headscale.client import HeadscaleApi, HsNode, HsPreAuthKey

if TYPE_CHECKING:
    from src.infrastructure.database.uow import UnitOfWork
    from src.infrastructure.di import AppContainer

log = get_logger(__name__)

LOCK_KEY = "lan:policy-lock"
_KEY_LOCK = "lan:key-lock:{}"  # per subscription, around pre-auth key issuance
# A crashed holder can't wedge LAN for longer than the TTL. Every Headscale call has its own
# 15 s deadline (client.CALL_DEADLINE), and the whole work under a lock is cut off at
# _LOCK_WORK — well below the TTL, so a lock never expires while its holder still runs.
_LOCK_TTL = 150
_LOCK_WORK = 120.0
_LOCK_WAIT = 10.0
NODES_CACHE_KEY = "lan:nodes"
_NODES_CACHE_TTL = 5  # seconds — the LAN tab polls rooms; one Headscale listing serves all
# Compare-and-delete: release a lock only if it still carries OUR token.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)


class LanBusy(HeadscaleError):
    """Another change held the LAN lock for too long (answered 503 as well)."""


class DeviceLimit(Exception):
    """The subscription already has ``max_devices`` devices (registered + a pending key)."""


@dataclass(frozen=True, slots=True)
class LanConfig:
    enabled: bool
    login_server: str
    api_key: str
    max_room_members: int
    max_rooms_owned: int
    max_rooms_joined: int
    max_devices: int

    @property
    def configured(self) -> bool:
        return bool(valid_login_server(self.login_server) and self.api_key)


def valid_login_server(url: str) -> bool:
    """``https://host[:port]`` — no path, query, credentials: the API key rides on it."""
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # raises on a garbage port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
    )


async def load_config(container: AppContainer) -> LanConfig:
    async with container.uow() as uow:
        snap = await container.bot_config.snapshot(uow)
    return LanConfig(
        enabled=bool(snap["LAN_ENABLED"]),
        login_server=str(snap["LAN_LOGIN_SERVER"] or "").strip().rstrip("/"),
        api_key=str(snap["LAN_HEADSCALE_API_KEY"] or "").strip(),
        max_room_members=max(2, int(snap["LAN_MAX_ROOM_MEMBERS"] or 8)),
        max_rooms_owned=max(1, int(snap["LAN_MAX_ROOMS_OWNED"] or 3)),
        max_rooms_joined=max(1, int(snap["LAN_MAX_ROOMS_JOINED"] or 10)),
        max_devices=max(1, int(snap["LAN_MAX_DEVICES"] or 5)),
    )


def settings_guard(changes: dict[str, Any], current_server: str) -> list[str]:
    """Admin settings PATCH: ``LAN_LOGIN_SERVER`` must be ``https://`` (normalised in place);
    a new server invalidates the saved API key (it belongs to the old one) unless the same
    PATCH brings a new key. Returns the keys to reset. Raises ValueError on a bad URL."""
    if "LAN_LOGIN_SERVER" not in changes:
        return []
    server = str(changes["LAN_LOGIN_SERVER"] or "").strip().rstrip("/")
    if server and not valid_login_server(server):
        raise ValueError("LAN_LOGIN_SERVER: нужен адрес вида https://lan.example.ru")
    changes["LAN_LOGIN_SERVER"] = server
    new_key = changes.get("LAN_HEADSCALE_API_KEY")
    if server != current_server.strip().rstrip("/") and new_key in (None, "", SECRET_MASK):
        changes.pop("LAN_HEADSCALE_API_KEY", None)
        return ["LAN_HEADSCALE_API_KEY"]
    return []


def open_headscale(container: AppContainer, cfg: LanConfig) -> HeadscaleApi:
    """A client for the configured server (use as ``async with``). Not configured → raises."""
    if not cfg.configured:
        raise HeadscaleError("headscale is not configured")
    return container.headscale_factory(cfg.login_server, cfg.api_key)


# --- locks -------------------------------------------------------------------------------
@asynccontextmanager
async def redis_mutex(redis: Any, key: str, *, wait: float | None = None) -> AsyncIterator[None]:
    """Wait up to ``wait`` s (default 10) for ``key``; released only by its owner (a token,
    atomic compare-and-delete), so a lock that expired and was retaken isn't dropped."""
    token = secrets.token_hex(8)
    deadline = time.monotonic() + (_LOCK_WAIT if wait is None else wait)
    while not await redis.set(key, token, nx=True, ex=_LOCK_TTL):
        if time.monotonic() >= deadline:
            raise LanBusy("lan lock is busy")
        await asyncio.sleep(0.1)
    try:
        async with asyncio.timeout(_LOCK_WORK):
            yield
    except TimeoutError as exc:
        raise HeadscaleError("lan operation exceeded the lock deadline") from exc
    finally:
        with suppress(Exception):
            await redis.eval(_RELEASE_LUA, 1, key, token)


def policy_lock(redis: Any) -> Any:
    """Serialise every room change, account creation and policy PUT."""
    return redis_mutex(redis, LOCK_KEY)


async def policy_from_db(uow: UnitOfWork) -> dict[str, Any]:
    return build_policy((await uow.lan_members.all_by_room()).values())


async def _restore_policy(container: AppContainer, cfg: LanConfig) -> None:
    """Best-effort re-PUT of the committed DB policy (caller holds the policy lock)."""
    with suppress(Exception):
        async with container.uow() as uow:
            policy = await policy_from_db(uow)
        async with open_headscale(container, cfg) as hs:
            await hs.set_policy(policy_json(policy))


@asynccontextmanager
async def changing_rooms(
    container: AppContainer, cfg: LanConfig, *, push: bool = True
) -> AsyncIterator[UnitOfWork]:
    """Edit rooms in the yielded UoW; on a clean exit the policy is rebuilt from that state,
    PUT to Headscale, and only then committed (see the module docstring). An exception in
    the body — or a failed PUT — rolls everything back. ``push=False``: a change that can't
    affect the policy (a new room code) — committed under the same lock, no PUT."""
    async with policy_lock(container.redis):
        put_attempted = False
        try:
            async with container.uow() as uow:
                yield uow
                await uow.flush()
                if push:
                    policy = await policy_from_db(uow)
                    put_attempted = True
                    async with open_headscale(container, cfg) as hs:
                        await hs.set_policy(policy_json(policy))
                await uow.commit()
        except Exception:
            if put_attempted:  # the PUT may have landed before the failure — undo it
                await _restore_policy(container, cfg)
            raise


async def unique_code(uow: UnitOfWork) -> str:
    for _ in range(20):
        code = generate_code()
        if await uow.lan_rooms.by_code(code) is None:
            return code
    raise RuntimeError("could not generate a free LAN room code")


# --- device keys ---------------------------------------------------------------------------
async def ensure_policy(container: AppContainer, cfg: LanConfig, subscription_id: int) -> str:
    """Under the policy lock: the subscription's account (created on first use) and a server
    policy equal to the DB one — PUT if missing/different. Any failure → HeadscaleError
    (no key may be issued while the server could still be allow-all). Returns the hs user."""
    async with policy_lock(container.redis), container.uow() as uow:
        account = await uow.lan_accounts.ensure(subscription_id)
        policy = await policy_from_db(uow)
        async with open_headscale(container, cfg) as hs:
            current = ""
            with suppress(HeadscaleError):  # nothing stored yet → PUT below decides
                current = await hs.get_policy()
            if not same_policy(current, policy):
                await hs.set_policy(policy_json(policy))
        await uow.commit()
        return account.hs_user


async def issue_device_key(
    container: AppContainer, cfg: LanConfig, subscription_id: int
) -> tuple[HsPreAuthKey, str]:
    """A single-use pre-auth key + the device name the app must use. One issuance per
    subscription at a time; earlier unused keys are expired first, so live devices + pending
    keys can never exceed ``max_devices``. Raises DeviceLimit / HeadscaleError."""
    async with redis_mutex(container.redis, _KEY_LOCK.format(subscription_id)):
        hs_user = await ensure_policy(container, cfg, subscription_id)
        now = dt.datetime.now(dt.UTC)
        async with open_headscale(container, cfg) as hs:
            user = await hs.ensure_user(hs_user)
            pending = [k for k in await hs.list_preauth_keys(user.id) if k.is_pending(now)]
            for old in pending:
                await hs.expire_preauth_key(old.id)
            live = [n for n in await own_nodes(hs, hs_user) if not n.is_expired(now)]
            if len(live) >= cfg.max_devices:
                raise DeviceLimit
            key = await hs.create_preauth_key(user.id, expires_at=now + PREAUTH_KEY_TTL)
    return key, new_hostname()


async def expire_subscription_nodes(container: AppContainer, subscription_id: int) -> None:
    """Best-effort (reset-link): the old link may be in someone else's hands — expire every
    node and pending key of the subscription; the owner re-registers with the new link."""
    try:
        cfg = await load_config(container)
        if not cfg.configured:
            return
        async with container.uow() as uow:
            hs_user = await uow.lan_accounts.hs_user_of(subscription_id)
        if hs_user is None:
            return
        async with open_headscale(container, cfg) as hs:
            users = await hs.list_users(hs_user)
            if not users:
                return
            for key in await hs.list_preauth_keys(users[0].id):
                if key.is_pending():
                    await hs.expire_preauth_key(key.id)
            for node in await own_nodes(hs, hs_user):
                if not node.is_expired():
                    await hs.expire_node(node.id)
    except Exception as exc:
        log.warning("lan: expiring nodes after reset-link failed", error=str(exc))


# --- views ---------------------------------------------------------------------------------
def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def device_view(node: HsNode) -> dict[str, Any]:
    return {
        "node_id": node.id,
        "name": node.name,
        "ip": node.ipv4,
        "online": node.online,
        "last_seen": _iso(node.last_seen),
        "expired": node.is_expired(),
    }


def _node_to_json(node: HsNode) -> dict[str, Any]:
    raw = dataclasses.asdict(node)
    for k in ("last_seen", "expiry", "created_at"):
        raw[k] = _iso(raw[k])
    raw["ips"] = list(node.ips)
    return raw


def _node_from_json(raw: dict[str, Any]) -> HsNode:
    def ts(v: Any) -> dt.datetime | None:
        return dt.datetime.fromisoformat(v) if v else None

    return HsNode(
        id=int(raw["id"]),
        name=str(raw["name"]),
        user=str(raw["user"]),
        ips=tuple(raw["ips"]),
        online=bool(raw["online"]),
        last_seen=ts(raw["last_seen"]),
        expiry=ts(raw["expiry"]),
        created_at=ts(raw.get("created_at")),
        user_id=int(raw.get("user_id") or 0),
    )


async def cached_nodes(container: AppContainer, hs: HeadscaleApi) -> list[HsNode]:
    """All nodes, shared through Redis for 5 s (rooms polling must not hammer Headscale)."""
    with suppress(Exception):
        raw = await container.redis.get(NODES_CACHE_KEY)
        if raw:
            return [_node_from_json(n) for n in json.loads(raw)]
    nodes = await hs.list_nodes()
    with suppress(Exception):
        payload = json.dumps([_node_to_json(n) for n in nodes])
        await container.redis.set(NODES_CACHE_KEY, payload, ex=_NODES_CACHE_TTL)
    return nodes


async def rooms_view(
    container: AppContainer,
    cfg: LanConfig,
    subscription_id: int,
    *,
    only_room: int | None = None,
) -> list[dict[str, Any]]:
    """The subscription's rooms with members' live devices. Members are labelled by seat
    (``Игрок N``, join order) — never a name, e-mail or Telegram id; ``member_id`` is the
    membership row, not a subscription id; devices show only the bot-issued ``cw-…`` name;
    the join code is shown to the owner only."""
    async with container.uow() as uow:
        rooms = [
            r
            for r in await uow.lan_rooms.for_subscription(subscription_id)
            if only_room is None or r.id == only_room
        ]
        members = {r.id: list(await uow.lan_members.of_room(r.id)) for r in rooms}
        sub_ids = {m.subscription_id for ms in members.values() for m in ms}
        accounts = {s: await uow.lan_accounts.hs_user_of(s) for s in sub_ids}
    # A member without an account has no Headscale identity at all — not shown (every
    # create/join makes one, so only a half-migrated row could lack it).
    members = {
        rid: [m for m in ms if accounts.get(m.subscription_id)] for rid, ms in members.items()
    }
    nodes: dict[str, list[HsNode]] = {}
    if rooms:
        async with open_headscale(container, cfg) as hs:
            for node in await cached_nodes(container, hs):
                if is_lan_user(node.user) and not node.is_expired():
                    nodes.setdefault(node.user, []).append(node)
    out: list[dict[str, Any]] = []
    for room in rooms:
        is_owner = room.owner_subscription_id == subscription_id
        out.append(
            {
                "id": room.id,
                "code": room.code if is_owner else None,
                "name": room.name,
                "is_owner": is_owner,
                "members": [
                    {
                        "member_id": m.id,
                        "label": member_label(i),
                        "devices": [
                            {"name": public_device_name(n.name), "ip": n.ipv4, "online": n.online}
                            for n in nodes.get(accounts.get(m.subscription_id) or "", [])
                        ],
                        "is_self": m.subscription_id == subscription_id,
                    }
                    for i, m in enumerate(members[room.id], start=1)
                ],
            }
        )
    return out


async def own_nodes(hs: HeadscaleApi, hs_user: str | None) -> list[HsNode]:
    """Nodes of one Headscale user, matched by the user's ID (not the name); none while the
    user doesn't exist (listing nodes of an unknown user is a server error in Headscale)."""
    users = await hs.list_users(hs_user) if hs_user else []
    if not users:
        return []
    return [n for n in await hs.list_nodes(hs_user) if n.user_id == users[0].id]


# --- periodic cleanup ----------------------------------------------------------------------
async def live_subscriptions(
    uow: UnitOfWork, sub_ids: Sequence[int] | set[int], now: dt.datetime
) -> set[int]:
    """Which of ``sub_ids`` may use LAN now (usable, not expired, account not blocked)."""
    if not sub_ids:
        return set()
    rows = await uow.session.execute(
        select(Subscription.id, Subscription.status, Subscription.expire_at, User.status)
        .join(User, User.id == Subscription.user_id)
        .where(Subscription.id.in_(list(sub_ids)))
    )
    return {
        int(sid)
        for sid, status, expire_at, user_status in rows.all()
        if subscription_lan_active(
            status.is_usable, expire_at, now, user_blocked=user_status is UserStatus.BLOCKED
        )
    }


async def _drop_inactive_members(uow: UnitOfWork, now: dt.datetime) -> int:
    member_subs = {
        int(s) for s in (await uow.session.scalars(select(LanRoomMember.subscription_id))).all()
    }
    gone = member_subs - await live_subscriptions(uow, member_subs, now)
    if not gone:
        return 0
    removed = await uow.lan_members.count_in(gone)
    owned = await uow.session.scalars(
        select(LanRoom).where(LanRoom.owner_subscription_id.in_(gone))
    )
    for room in list(owned):
        await uow.lan_rooms.remove(room)  # the owner left → the room goes (as on «leave»)
    await uow.session.execute(delete(LanRoomMember).where(LanRoomMember.subscription_id.in_(gone)))
    await uow.flush()
    return removed


def _newest_first(node: HsNode) -> tuple[dt.datetime, int]:
    return (node.created_at or dt.datetime.min.replace(tzinfo=dt.UTC), node.id)


async def cleanup(container: AppContainer, now: dt.datetime | None = None) -> dict[str, int]:
    """Lapsed (or blocked) subscriptions leave their rooms (rooms they own are deleted) and
    the DB policy is PUT — on every tick, unconditionally. Then, over our users' nodes:
    lapsed/unknown ones are expired, devices above ``max_devices`` are deleted newest first,
    and nodes not named by us get a ``cw-…`` name. Headscale down → nothing is committed."""
    now = now or dt.datetime.now(dt.UTC)
    stats = {"removed": 0, "policy_pushed": 0, "expired_nodes": 0, "deleted": 0, "renamed": 0}
    cfg = await load_config(container)
    if not cfg.configured:
        return stats
    try:
        async with policy_lock(container.redis), container.uow() as uow:
            removed = await _drop_inactive_members(uow, now)
            policy = await policy_from_db(uow)
            async with open_headscale(container, cfg) as hs:
                await hs.set_policy(policy_json(policy))
            await uow.commit()
        stats["removed"], stats["policy_pushed"] = removed, 1

        async with open_headscale(container, cfg) as hs:
            live = [
                n for n in await hs.list_nodes() if is_lan_user(n.user) and not n.is_expired(now)
            ]
            async with container.uow() as uow:
                owner = await uow.lan_accounts.by_hs_users(sorted({n.user for n in live}))
                active = await live_subscriptions(uow, set(owner.values()), now)
            keep: dict[str, list[HsNode]] = {}
            for node in live:
                if owner.get(node.user) in active:
                    keep.setdefault(node.user, []).append(node)
                else:  # lapsed, blocked, or a subscription that no longer exists
                    await hs.expire_node(node.id)
                    stats["expired_nodes"] += 1
            for nodes in keep.values():
                nodes.sort(key=_newest_first, reverse=True)
                extra = len(nodes) - cfg.max_devices
                for node in nodes[: max(0, extra)]:
                    await hs.delete_node(node.id)
                    stats["deleted"] += 1
                for node in nodes[max(0, extra) :]:
                    if not is_our_hostname(node.name):
                        await hs.rename_node(node.id, new_hostname())
                        stats["renamed"] += 1
    except HeadscaleError as exc:
        log.warning("lan cleanup skipped: headscale unavailable", error=str(exc), **stats)
        return stats
    if any(v for k, v in stats.items() if k != "policy_pushed"):
        log.info("lan cleanup", **stats)
    return stats


async def check_connection(container: AppContainer) -> dict[str, Any]:
    """Admin «Проверить соединение»: health, users, nodes, and a TRIAL PUT of the DB policy
    (file mode / no rights must surface here, not when a customer asks for a key). Short
    Russian reasons only — never the server's response body."""
    cfg = await load_config(container)
    if not cfg.configured:
        return {"ok": False, "error": "Не заданы адрес Headscale (https://) или API-ключ"}
    try:
        async with open_headscale(container, cfg) as hs:
            database = await hs.health()
            users = await hs.list_users()
            nodes = await hs.list_nodes()
        async with policy_lock(container.redis), container.uow() as uow:
            policy = await policy_from_db(uow)
            async with open_headscale(container, cfg) as hs:
                with suppress(HeadscaleError):
                    await hs.get_policy()
                await hs.set_policy(policy_json(policy))
    except HeadscalePolicyReadOnly:
        return {
            "ok": False,
            "error": "policy.mode не database — бот не может записать политику доступа",
        }
    except HeadscaleAuthError:
        return {"ok": False, "error": "Headscale отклонил API-ключ"}
    except LanBusy:
        return {"ok": False, "error": "Идёт другое изменение LAN — повторите через минуту"}
    except HeadscaleError as exc:
        if exc.status is None:
            return {"ok": False, "error": "Headscale недоступен"}
        return {"ok": False, "error": f"Headscale ответил ошибкой {exc.status}"}
    return {
        "ok": True,
        "database": database,
        "users": len(users),
        "nodes": len(nodes),
        "online": sum(1 for n in nodes if n.online),
        "policy": "записана",
    }
