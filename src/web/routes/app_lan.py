"""LAN Play API for the Windows app (``/api/app/v1/lan``) — rooms by code over our Headscale.

Same credential as the rest of ``/api/app/v1``: ``X-Subscription-Token`` (the panel-confirmed
subscription link token, see app_api.py). The subscription is one Headscale user with a random
name (``lan_accounts``); the app never talks to the Headscale API — it only gets a short-lived
single-use pre-auth key AND the device name to use (``cw-<hex>``) here, and logs its daemon in
with ``--login-server``.

Rules: LAN switched off (``LAN_ENABLED``) → 404 ``lan_disabled`` (``/status`` answers
``enabled: false``); a subscription that isn't live → 403 ``subscription_inactive``; Headscale
unreachable → 503 ``lan_unavailable`` and the change is NOT applied (DB and policy move
together — see ``infrastructure/services/lan.py``); no key is issued unless the server holds
our policy. Joining by code is rate-limited per subscription and per IP against brute force;
a kicked member is banned from that room and the code is rotated. Members are «Игрок N»,
devices are shown by the bot-issued name only, the code is shown to the owner only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.application.services.lan import clean_room_name, normalize_code, subscription_lan_active
from src.core.exceptions import HeadscaleError
from src.core.logging import get_logger
from src.infrastructure.database.models.lan_room import LanRoom, LanRoomBan, LanRoomMember
from src.infrastructure.di import AppContainer
from src.infrastructure.services.lan import (
    DeviceLimit,
    LanBusy,
    LanConfig,
    changing_rooms,
    device_view,
    issue_device_key,
    load_config,
    open_headscale,
    own_nodes,
    rooms_view,
    unique_code,
)
from src.web.deps import get_container
from src.web.routes.app_api import AppIdentity, _ip_bucket, app_identity
from src.web.routes.cabinet_auth import _client_ip, _rate_limit

if TYPE_CHECKING:
    from src.infrastructure.database.uow import UnitOfWork

log = get_logger(__name__)

router = APIRouter(prefix="/api/app/v1/lan", tags=["app"])

# Join attempts (right or wrong code) per 10 minutes: per subscription, per IP (/64 for v6).
_JOIN_LIMIT_SUB = 10
_JOIN_LIMIT_IP = 30
_JOIN_WINDOW = 600
# Pre-auth keys per subscription per hour (each is single-use and lives 10 minutes).
_KEY_LIMIT = 20
# Global brake: wrong codes across ALL subscriptions per hour (many trial subscriptions x many
# IPs would slip under the per-sub/per-IP limits). Over it, joins answer 503 ``lan_busy`` until
# the window ends, and admins get one alert per hour.
_JOIN_FAIL_GLOBAL = 200
_JOIN_FAIL_WINDOW = 3600
_JOIN_FAIL_KEY = "lan:join-fail"
_JOIN_ALERT_KEY = "lan:join-fail-alert"


@dataclass(frozen=True)
class LanCaller:
    ident: AppIdentity
    cfg: LanConfig

    @property
    def sub_id(self) -> int:
        return self.ident.subscription_id


async def _require_active(container: AppContainer, ident: AppIdentity) -> None:
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(ident.subscription_id)
    if sub is None or not subscription_lan_active(sub.is_usable, sub.expire_at):
        raise HTTPException(403, "subscription_inactive")


def lan_caller(kind: str) -> Callable[..., Awaitable[LanCaller]]:
    """Dependency: token (blocked accounts already refused) → LAN on → subscription live."""
    identity = app_identity(kind)

    async def dependency(
        ident: AppIdentity = Depends(identity),
        container: AppContainer = Depends(get_container),
    ) -> LanCaller:
        cfg = await load_config(container)
        if not cfg.enabled:
            raise HTTPException(404, "lan_disabled")
        await _require_active(container, ident)
        return LanCaller(ident=ident, cfg=cfg)

    return dependency


@contextmanager
def _headscale_errors() -> Iterator[None]:
    """Headscale down / key rejected / lock busy → a clean 503 (nothing was applied)."""
    try:
        yield
    except LanBusy as exc:
        raise HTTPException(503, "lan_busy") from exc
    except HeadscaleError as exc:
        log.warning("lan request failed: headscale unavailable", error=str(exc))
        raise HTTPException(503, "lan_unavailable") from exc


async def _join_brake(container: AppContainer) -> None:
    try:
        n = int(await container.redis.get(_JOIN_FAIL_KEY) or 0)
    except Exception:  # fail-open like the other limiters
        return
    if n >= _JOIN_FAIL_GLOBAL:
        raise HTTPException(503, "lan_busy")


async def _join_failed(container: AppContainer) -> None:
    try:
        n = int(await container.redis.incr(_JOIN_FAIL_KEY))
        if n == 1:
            await container.redis.expire(_JOIN_FAIL_KEY, _JOIN_FAIL_WINDOW)
        alert = n >= _JOIN_FAIL_GLOBAL and bool(
            await container.redis.set(_JOIN_ALERT_KEY, "1", nx=True, ex=_JOIN_FAIL_WINDOW)
        )
    except Exception:
        return
    if alert:
        log.warning("lan: join brake engaged", failures=n)
        with suppress(Exception):
            await container.notifier.notify_admins(
                f"🚨 LAN для игр: за час {n} попыток войти в комнату с неверным кодом "
                "(похоже на перебор). Вход по коду временно закрыт до конца часа.",
                topic="alerts",
            )


async def _one_room(container: AppContainer, caller: LanCaller, room_id: int) -> dict[str, Any]:
    with _headscale_errors():
        rooms = await rooms_view(container, caller.cfg, caller.sub_id, only_room=room_id)
    if not rooms:
        raise HTTPException(404, "room_not_found")
    return rooms[0]


async def _my_room(uow: UnitOfWork, room_id: int, sub_id: int) -> tuple[LanRoom, LanRoomMember]:
    """The room and the caller's membership — 404 for a room they aren't in (no probing)."""
    room = await uow.lan_rooms.get(room_id)
    me = await uow.lan_members.find_one(room_id=room_id, subscription_id=sub_id)
    if room is None or me is None:
        raise HTTPException(404, "room_not_found")
    return room, me


async def _hs_user(container: AppContainer, sub_id: int) -> str | None:
    async with container.uow() as uow:
        return await uow.lan_accounts.hs_user_of(sub_id)


# --- status / devices ------------------------------------------------------------------------
@router.get("/status")
async def lan_status(
    ident: AppIdentity = Depends(app_identity("lan")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    cfg = await load_config(container)
    if cfg.enabled:
        await _require_active(container, ident)
    return {
        "enabled": cfg.enabled,
        "login_server": cfg.login_server if cfg.enabled else None,
        "max_room_members": cfg.max_room_members,
        "max_rooms_owned": cfg.max_rooms_owned,
        "max_rooms_joined": cfg.max_rooms_joined,
        "max_devices": cfg.max_devices,
    }


class DeviceIn(BaseModel):
    hostname: str = Field("", max_length=63)  # accepted for compatibility, not used


@router.post("/device")
async def lan_device(
    body: DeviceIn,
    caller: LanCaller = Depends(lan_caller("lan-post")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """A single-use pre-auth key (10 min, not ephemeral, tag-less) plus the device name the
    app passes to the daemon (``--hostname``). Earlier unused keys are expired; 409
    ``device_limit`` at ``max_devices`` live nodes — the app frees a slot with DELETE
    /device/{node_id}. 503 if the server's policy can't be confirmed (fail closed)."""
    await _rate_limit(container, "lan-key", str(caller.sub_id), limit=_KEY_LIMIT, window=3600)
    with _headscale_errors():
        try:
            key, hostname = await issue_device_key(container, caller.cfg, caller.sub_id)
        except DeviceLimit as exc:
            raise HTTPException(409, "device_limit") from exc
    return {
        "login_server": caller.cfg.login_server,
        "auth_key": key.key,
        "expires_at": key.expiration.isoformat() if key.expiration else None,
        "hostname": hostname,
    }


@router.get("/devices")
async def lan_devices(
    caller: LanCaller = Depends(lan_caller("lan")),
    container: AppContainer = Depends(get_container),
) -> list[dict[str, Any]]:
    hs_user = await _hs_user(container, caller.sub_id)
    if hs_user is None:
        return []
    with _headscale_errors():
        async with open_headscale(container, caller.cfg) as hs:
            nodes = await own_nodes(hs, hs_user)
    return [device_view(n) for n in nodes]


@router.delete("/device/{node_id}")
async def lan_device_delete(
    node_id: int,
    caller: LanCaller = Depends(lan_caller("lan-post")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    hs_user = await _hs_user(container, caller.sub_id)
    with _headscale_errors():
        async with open_headscale(container, caller.cfg) as hs:
            if node_id not in {n.id for n in await own_nodes(hs, hs_user)}:
                raise HTTPException(404, "device_not_found")
            await hs.delete_node(node_id)
    return {"ok": True}


# --- rooms ---------------------------------------------------------------------------------
@router.get("/rooms")
async def lan_rooms(
    caller: LanCaller = Depends(lan_caller("lan")),
    container: AppContainer = Depends(get_container),
) -> list[dict[str, Any]]:
    with _headscale_errors():
        return await rooms_view(container, caller.cfg, caller.sub_id)


class RoomIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


@router.post("/rooms")
async def lan_room_create(
    body: RoomIn,
    caller: LanCaller = Depends(lan_caller("lan-post")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    name = clean_room_name(body.name)
    if not name:
        raise HTTPException(422, "room_name_required")
    with _headscale_errors():
        async with changing_rooms(container, caller.cfg) as uow:
            owned = await uow.lan_rooms.count(owner_subscription_id=caller.sub_id)
            if owned >= caller.cfg.max_rooms_owned:
                raise HTTPException(409, "room_limit")
            if await uow.lan_members.memberships(caller.sub_id) >= caller.cfg.max_rooms_joined:
                raise HTTPException(409, "membership_limit")
            await uow.lan_accounts.ensure(caller.sub_id)
            room = await uow.lan_rooms.add(
                LanRoom(
                    code=await unique_code(uow),
                    name=name,
                    owner_subscription_id=caller.sub_id,
                )
            )
            await uow.lan_members.add(LanRoomMember(room_id=room.id, subscription_id=caller.sub_id))
            room_id = room.id
    return await _one_room(container, caller, room_id)


class JoinIn(BaseModel):
    code: str = Field(..., max_length=32)


@router.post("/rooms/join")
async def lan_room_join(
    body: JoinIn,
    request: Request,
    caller: LanCaller = Depends(lan_caller("lan-post")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    ip = _ip_bucket(_client_ip(request))
    await _rate_limit(
        container, "lan-join", str(caller.sub_id), limit=_JOIN_LIMIT_SUB, window=_JOIN_WINDOW
    )
    await _rate_limit(container, "lan-join-ip", ip, limit=_JOIN_LIMIT_IP, window=_JOIN_WINDOW)
    await _join_brake(container)
    code = normalize_code(body.code)
    if code is not None:
        async with container.uow() as uow:  # a wrong guess never takes the policy lock
            if await uow.lan_rooms.by_code(code) is None:
                code = None
    if code is None:
        await _join_failed(container)
        raise HTTPException(404, "room_not_found")
    with _headscale_errors():
        async with changing_rooms(container, caller.cfg) as uow:
            room = await uow.lan_rooms.by_code(code)
            if room is None:  # rotated / deleted meanwhile
                raise HTTPException(404, "room_not_found")
            room_id = room.id
            already = await uow.lan_members.find_one(room_id=room.id, subscription_id=caller.sub_id)
            if already is None:
                if await uow.lan_bans.is_banned(room.id, caller.sub_id):
                    raise HTTPException(403, "banned")
                if await uow.lan_members.memberships(caller.sub_id) >= caller.cfg.max_rooms_joined:
                    raise HTTPException(409, "membership_limit")
                if await uow.lan_members.count(room_id=room.id) >= caller.cfg.max_room_members:
                    raise HTTPException(409, "room_full")
                await uow.lan_accounts.ensure(caller.sub_id)
                await uow.lan_members.add(
                    LanRoomMember(room_id=room.id, subscription_id=caller.sub_id)
                )
    return await _one_room(container, caller, room_id)


@router.post("/rooms/{room_id}/leave")
async def lan_room_leave(
    room_id: int,
    caller: LanCaller = Depends(lan_caller("lan-post")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Leave a room; the owner leaving deletes it for everyone."""
    with _headscale_errors():
        async with changing_rooms(container, caller.cfg) as uow:
            room, me = await _my_room(uow, room_id, caller.sub_id)
            deleted = room.owner_subscription_id == caller.sub_id
            if deleted:
                await uow.lan_rooms.remove(room)
            else:
                await uow.lan_members.delete(me)
    return {"ok": True, "room_deleted": deleted}


class KickIn(BaseModel):
    member_id: int


@router.post("/rooms/{room_id}/kick")
async def lan_room_kick(
    room_id: int,
    body: KickIn,
    caller: LanCaller = Depends(lan_caller("lan-post")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Owner only. The member is removed AND banned from this room, and the code is rotated
    in the same transaction (they may have shared it)."""
    with _headscale_errors():
        async with changing_rooms(container, caller.cfg) as uow:
            room, _ = await _my_room(uow, room_id, caller.sub_id)
            if room.owner_subscription_id != caller.sub_id:
                raise HTTPException(403, "not_owner")
            member = await uow.lan_members.get(body.member_id)
            if member is None or member.room_id != room.id:
                raise HTTPException(404, "member_not_found")
            if member.subscription_id == caller.sub_id:
                raise HTTPException(400, "cannot_kick_self")
            if not await uow.lan_bans.is_banned(room.id, member.subscription_id):
                await uow.lan_bans.add(
                    LanRoomBan(room_id=room.id, subscription_id=member.subscription_id)
                )
            await uow.lan_members.delete(member)
            room.code = await unique_code(uow)
    return await _one_room(container, caller, room_id)


@router.post("/rooms/{room_id}/regenerate-code")
async def lan_room_regenerate_code(
    room_id: int,
    caller: LanCaller = Depends(lan_caller("lan-post")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """A new code (the old one stops working); members stay — the policy doesn't change."""
    with _headscale_errors():
        async with changing_rooms(container, caller.cfg, push=False) as uow:
            room, _ = await _my_room(uow, room_id, caller.sub_id)
            if room.owner_subscription_id != caller.sub_id:
                raise HTTPException(403, "not_owner")
            room.code = await unique_code(uow)
    return await _one_room(container, caller, room_id)
