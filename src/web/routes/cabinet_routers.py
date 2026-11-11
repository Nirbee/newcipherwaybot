"""Cabinet: «Мой роутер» — the customer's own switches for their home router.

Two things a customer may change, only on a router whose subscription is theirs:
* «Напрямую»: the home's traffic bypasses VPN for a while (router keeps measuring);
* the location: a pinned primary server, AUTO routers only (on FORCE ones an admin sets it).

Each change alters the config the agent applies, and applying it restarts Xray — the whole home
loses internet for a few seconds. Hence the rate limits below, and why a repeat of the current
state is answered without touching anything. Server identity stays server-side: a location is
an opaque id (router_config.location_id), never a panel uuid or address.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import replace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.application.dto.panel import PanelHost
from src.application.services.router_config import (
    eligible_stamp,
    location_hosts,
    location_id,
    proxy_tag,
)
from src.core.enums import RouterDeviceMode, RouterDeviceStatus
from src.core.logging import get_logger
from src.infrastructure.database.models.router_device import RouterDevice
from src.infrastructure.database.models.user import User
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.agent import (
    config_info,
    device_is_direct,
    device_is_online,
    device_revision,
    location_pings,
    subscription_live,
)
from src.web.routes.cabinet import cabinet_user

log = get_logger(__name__)
router = APIRouter(prefix="/api/cabinet/routers", tags=["cabinet"])

_DIRECT_PER_HOUR = 20
_LOCATION_PER_HOUR = 6
# A customer with several routers legitimately flips each of them; one router is one home.
_USER_MULTIPLIER = 3
_HOUR = 3600
_READS_PER_MINUTE = 120  # the «применяется…» poll is every few seconds, per open cabinet
# A failed probe stops being a reason to grey a location out once it's this old.
_PING_FRESH_SECONDS = 2 * 3600
_HOSTS_TTL_SECONDS = 60
_SQUADS_TTL_SECONDS = 300
_ELIGIBLE_KEY = "ROUTER_ELIGIBLE_HOSTS"


class DirectIn(BaseModel):
    duration: Literal["1h", "today", "forever", "off"]
    # Minutes east of UTC, as the client's clock says (MSK = 180) — «today» ends at its midnight.
    tz_offset_minutes: int | None = Field(None, ge=-12 * 60, le=14 * 60)


class LocationIn(BaseModel):
    host: str | None = Field(None, max_length=32)  # an id from the list, null = «Авто»


async def _throttle(
    container: AppContainer, bucket: str, ident: str, *, limit: int, window: int, detail: str
) -> None:
    """Fixed-window limiter, fail-open like every other limiter here: a Redis hiccup must not
    lock a customer out of their own router."""
    key = f"rl:router:{bucket}:{ident}"
    try:
        hits = int(await container.redis.incr(key))
        # INCR and EXPIRE are two calls: if the EXPIRE was lost, the key would never expire and
        # the customer could lose the «VPN on» button for good — re-arm it on the next hit.
        if hits == 1 or int(await container.redis.ttl(key)) == -1:
            await container.redis.expire(key, window)
    except Exception:
        return
    if hits > limit:
        raise HTTPException(429, detail)


async def _load_hosts(container: AppContainer) -> list[PanelHost]:
    """The panel's hosts, cached briefly: this is read on every poll of the panel page and
    must not turn into a panel request each time."""
    fresh_key, stale_key = "agent:hosts", "agent:hosts:last"
    try:
        cached = await container.redis.get(fresh_key)
    except Exception:
        cached = None
    if cached:
        try:
            return _decode_hosts(cached)
        except (TypeError, ValueError):
            pass  # another version's shape: fall through and refetch
    try:
        hosts = await container.remnawave_client.get_hosts()
    except Exception as exc:
        log.warning("cabinet routers: hosts unavailable", error=str(exc))
        try:
            stale = await container.redis.get(stale_key)
            return _decode_hosts(stale) if stale else []
        except Exception:
            return []
    encoded = json.dumps([dataclasses.asdict(h) for h in hosts])
    try:
        await container.redis.set(fresh_key, encoded, ex=_HOSTS_TTL_SECONDS)
        await container.redis.set(stale_key, encoded)
    except Exception:
        log.warning("cabinet routers: hosts cache write failed")
    return hosts


def _decode_hosts(raw: Any) -> list[PanelHost]:
    data = json.loads(raw)
    return [PanelHost(**{**h, "squad_uuids": tuple(h.get("squad_uuids") or ())}) for h in data]


async def _user_squads(container: AppContainer, sub: Any, user: User) -> tuple[str, ...]:
    """The subscription's internal squads, cached like the hosts. () = unknown (don't filter)."""
    key = f"agent:squads:{sub.id}"
    try:
        cached = await container.redis.get(key)
        if cached:
            return tuple(json.loads(cached))
    except Exception:
        pass
    panel_ref = sub.panel_ref
    if panel_ref is None:
        return ()
    try:
        panel_user = await container.remnawave_client.get_user(
            replace(panel_ref, telegram_id=user.telegram_id)
        )
    except Exception as exc:
        log.warning("cabinet routers: squads unavailable", error=str(exc))
        return ()
    squads = tuple(panel_user.internal_squads) if panel_user else ()
    try:
        await container.redis.set(key, json.dumps(squads), ex=_SQUADS_TTL_SECONDS)
    except Exception:
        log.warning("cabinet routers: squads cache write failed")
    return squads


async def _eligible(uow: UnitOfWork) -> set[str]:
    row = await uow.bot_config.find_one(key=_ELIGIBLE_KEY)
    return {str(v) for v in ((row.value if row is not None else None) or [])}


def _ping_fresh(ping: dict[str, Any] | None, now: dt.datetime) -> bool:
    if not ping or not ping.get("at"):
        return False
    try:
        at = dt.datetime.fromisoformat(str(ping["at"]))
    except ValueError:
        return False
    return (now - at).total_seconds() < _PING_FRESH_SECONDS


def _location_item(
    host: PanelHost, ping: dict[str, Any] | None, now: dt.datetime
) -> dict[str, Any]:
    fresh = _ping_fresh(ping, now)
    failed = ping is not None and fresh and not ping.get("ok")
    ms = (ping or {}).get("ms")
    return {
        "id": location_id(host.uuid),
        "name": host.remark,
        "ping_ms": int(ms) if isinstance(ms, int | float) and not failed else None,
        "available": not host.is_disabled and not failed,
        "measured_at": (ping or {}).get("at"),
    }


async def _router_item(
    container: AppContainer,
    device: RouterDevice,
    sub: Any,
    user: User,
    hosts: list[PanelHost],
    eligible: set[str],
    now: dt.datetime,
) -> dict[str, Any]:
    live = subscription_live(sub)
    can_choose = device.mode is RouterDeviceMode.AUTO
    direct_on = device_is_direct(device, now)
    locations: list[dict[str, Any]] = []
    location: dict[str, Any] = {"auto": True, "id": None, "name": None}
    if can_choose:
        squads = await _user_squads(container, sub, user)
        pings = await location_pings(container, device.id)
        order = {h.uuid: i for i, h in enumerate(hosts)}
        options = sorted(
            location_hosts(hosts, eligible, squads, include_disabled=True),
            key=lambda h: order.get(h.uuid, 0),
        )
        locations = [_location_item(h, pings.get(h.uuid), now) for h in options]
        pinned = device.pinned_host_uuid
        if pinned and pinned in eligible:
            host = next((h for h in hosts if h.uuid == pinned), None)
            location = {
                "auto": False,
                "id": location_id(pinned),
                "name": host.remark if host else None,
            }
    # «Авто» / admin-set: the server the router actually got as its main one.
    if location["auto"]:
        info = await config_info(container, device.id) or {}
        primary = device.primary_host_uuid
        tags = info.get("tags") or {}
        location["name"] = tags.get(proxy_tag(primary)) if primary else None
    return {
        "id": device.id,
        "label": device.label,
        "online": device_is_online(device),
        "can_manage": live,
        "can_choose_location": can_choose,
        "vpn_mode": "direct" if direct_on else "vpn",
        "direct_until": (
            device.direct_until.isoformat()
            if direct_on and not device.direct_forever and device.direct_until
            else None
        ),
        "direct_forever": direct_on and device.direct_forever,
        "location": location,
        "locations": locations,
        "applied": device.config_rev == device_revision(device, eligible_stamp(eligible), now),
    }


async def _owned_device(uow: UnitOfWork, user: User, device_id: int) -> tuple[RouterDevice, Any]:
    device = await uow.router_devices.get(device_id)
    sub = await uow.subscriptions.get(device.subscription_id) if device is not None else None
    if (
        device is None
        or sub is None
        or sub.user_id != user.id
        or device.status is RouterDeviceStatus.REVOKED
    ):
        raise HTTPException(404, "router not found")
    return device, sub


def _require_live(sub: Any) -> None:
    if not subscription_live(sub):
        raise HTTPException(403, "Нужна активная подписка — продлите её, чтобы управлять роутером")


@router.get("")
async def list_routers(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    await _throttle(
        container,
        "read",
        str(user.id),
        limit=_READS_PER_MINUTE,
        window=60,
        detail="Слишком много запросов — подождите минуту",
    )
    async with container.uow() as uow:
        devices = await uow.router_devices.list_for_user(user.id)
        if not devices:
            return {"items": []}
        subs = {d.subscription_id: await uow.subscriptions.get(d.subscription_id) for d in devices}
        eligible = await _eligible(uow)
    hosts = await _load_hosts(container)
    now = dt.datetime.now(dt.UTC)
    return {
        "items": [
            await _router_item(container, d, subs[d.subscription_id], user, hosts, eligible, now)
            for d in devices
            if subs.get(d.subscription_id) is not None
        ]
    }


def _end_of_local_day(now: dt.datetime, offset_minutes: int) -> dt.datetime:
    """UTC moment of the next midnight on the customer's clock, never more than 24 h ahead."""
    offset = dt.timedelta(minutes=offset_minutes)
    local = now + offset
    midnight = (local + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return min(midnight - offset, now + dt.timedelta(hours=24))


def _direct_state(device: RouterDevice) -> dict[str, Any]:
    return {
        "ok": True,
        "vpn_mode": "direct" if device_is_direct(device) else "vpn",
        "direct_until": device.direct_until.isoformat() if device.direct_until else None,
        "direct_forever": device.direct_forever,
    }


@router.post("/{device_id}/direct")
async def set_direct(
    device_id: int,
    body: DirectIn,
    user: User = Depends(cabinet_user),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    until: dt.datetime | None = None
    if body.duration == "today":
        if body.tz_offset_minutes is None:
            raise HTTPException(400, "Укажите часовой пояс")
        until = _end_of_local_day(now, body.tz_offset_minutes)
    elif body.duration == "1h":
        until = now + dt.timedelta(hours=1)

    async with container.uow() as uow:
        device, sub = await _owned_device(uow, user, device_id)
        _require_live(sub)
        if body.duration == "off" and not device_is_direct(device, now):
            return _direct_state(device)  # already on VPN: nothing to restart
        detail = "Слишком частые переключения — попробуйте позже"
        # «off» has its own allowance: a customer who used up the hour must still be able to
        # turn VPN back on.
        bucket = "direct-off" if body.duration == "off" else "direct"
        await _throttle(
            container, bucket, f"d{device_id}", limit=_DIRECT_PER_HOUR, window=_HOUR, detail=detail
        )
        await _throttle(
            container,
            bucket,
            f"u{user.id}",
            limit=_DIRECT_PER_HOUR * _USER_MULTIPLIER,
            window=_HOUR,
            detail=detail,
        )
        device.direct_forever = body.duration == "forever"
        device.direct_until = until
        await uow.commit()
        return _direct_state(device)


@router.post("/{device_id}/location")
async def set_location(
    device_id: int,
    body: LocationIn,
    user: User = Depends(cabinet_user),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    detail = "Локацию можно менять не чаще 6 раз в час — каждая смена ненадолго обрывает интернет"
    async with container.uow() as uow:
        device, sub = await _owned_device(uow, user, device_id)
        if device.mode is not RouterDeviceMode.AUTO:
            raise HTTPException(409, "Локацию этого роутера задаёт администратор")
        _require_live(sub)
        # Spends the panel requests below, valid or not.
        await _throttle(
            container, "location-try", str(user.id), limit=30, window=600, detail=detail
        )
        pinned_uuid: str | None = None
        if body.host is not None:
            eligible = await _eligible(uow)
            try:
                # Fresh, not the display cache: this is the check that decides.
                hosts = await container.remnawave_client.get_hosts()
                panel_ref = sub.panel_ref
                panel_user = (
                    await container.remnawave_client.get_user(
                        replace(panel_ref, telegram_id=user.telegram_id)
                    )
                    if panel_ref is not None
                    else None
                )
            except Exception as exc:
                raise HTTPException(503, "Сервис временно недоступен — попробуйте позже") from exc
            squads = tuple(panel_user.internal_squads) if panel_user else ()
            match = next(
                (
                    h
                    for h in location_hosts(hosts, eligible, squads)
                    if location_id(h.uuid) == body.host
                ),
                None,
            )
            if match is None:
                raise HTTPException(400, "Такой локации нет в списке")
            pinned_uuid = match.uuid
        if pinned_uuid == device.pinned_host_uuid:
            return {"ok": True}  # already there: a re-tap must not restart Xray
        await _throttle(
            container,
            "location",
            f"d{device_id}",
            limit=_LOCATION_PER_HOUR,
            window=_HOUR,
            detail=detail,
        )
        await _throttle(
            container,
            "location",
            f"u{user.id}",
            limit=_LOCATION_PER_HOUR * _USER_MULTIPLIER,
            window=_HOUR,
            detail=detail,
        )
        device.pinned_host_uuid = pinned_uuid
        await uow.commit()
    return {"ok": True}
