"""Router agent API: what a customer's XKeen/Xray router itself polls.

Deliberately NOT under /api/admin (staff-only JWT) — this is a separate, narrower surface
authenticated by one bearer token per RouterDevice (never a query param, so it can't end up in
a reverse-proxy access log). The token is looked up by its sha256 hash; the plain value only
ever existed in the admin API's create/rotate responses.

Panel-unavailable is NOT the same as "no hosts assigned"/"unpaid": a genuine failure to reach
Remnawave must 503 rather than silently hand back a freedom-only config — the agent keeps
whatever config it already has on a 503, but a 200 with an empty proxy list is taken as the
deliberate truth and applied. Collapsing those two states would mean a panel blip disables VPN
for the whole router fleet at once.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from src.application.services.router_config import build_outbounds, config_etag
from src.core.enums import RouterDeviceStatus
from src.core.logging import get_logger
from src.infrastructure.database.models.router_device import RouterDevice
from src.infrastructure.di import AppContainer
from src.web.deps import get_container

log = get_logger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

# A hair under the agent's 5-minute poll interval's floor — generous margin for a misbehaving
# script or clock drift, never tight enough to bite a legitimately-timed poll.
_RATE_LIMIT_SECONDS = 50
_LAST_ERROR_MAX = 512


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _rate_limit_ok(container: AppContainer, key: str) -> bool:
    try:
        return bool(await container.redis.set(key, "1", nx=True, ex=_RATE_LIMIT_SECONDS))
    except Exception:
        return True  # a redis hiccup must not block a legitimate poll


async def require_device(
    authorization: str | None = Header(None),
    container: AppContainer = Depends(get_container),
) -> RouterDevice:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "missing bearer token")
    async with container.uow() as uow:
        device = await uow.router_devices.by_token_hash(_hash_token(token))
    if device is None:
        raise HTTPException(401, "unknown token")
    if device.status is RouterDeviceStatus.REVOKED:
        raise HTTPException(403, "device revoked")
    return device


@router.get("/config")
async def get_config(
    response: Response,
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    device: RouterDevice = Depends(require_device),
    container: AppContainer = Depends(get_container),
) -> Any:
    if not await _rate_limit_ok(container, f"agent:cfg:{device.id}"):
        raise HTTPException(429, "too many requests")

    async with container.uow() as uow:
        sub = await uow.subscriptions.get(device.subscription_id)
        if sub is None:
            raise HTTPException(404, "subscription not found")
        owner = await uow.users.get(sub.user_id)

    panel_ref = sub.panel_ref
    vless_uuid = ""
    hosts: list[Any] = []
    if panel_ref is not None:
        # Same telegram_id-attachment every other panel_ref caller needs (see client.py's
        # _v3_id): a uuid/short_id-only ref can't resolve on a v3 panel without it.
        panel_ref = replace(panel_ref, telegram_id=owner.telegram_id if owner else None)
        try:
            panel_user = await container.remnawave_client.get_user(panel_ref)
            wanted = {u for u in (device.primary_host_uuid, device.backup_host_uuid) if u}
            if wanted:
                all_hosts = await container.remnawave_client.get_hosts()
                hosts = [h for h in all_hosts if h.uuid in wanted]
        except Exception as exc:
            log.warning("agent config: panel unavailable", device_id=device.id, error=str(exc))
            raise HTTPException(503, "panel temporarily unavailable") from exc
        vless_uuid = (panel_user.vless_uuid if panel_user else None) or ""

    config = build_outbounds(
        hosts, vless_uuid=vless_uuid, subscription_active=sub.status.is_usable
    )
    etag = config_etag(config)

    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=304)

    async with container.uow() as uow:
        fresh = await uow.router_devices.get(device.id)
        if fresh is not None:
            fresh.config_etag = etag
            await uow.commit()

    response.headers["ETag"] = f'"{etag}"'
    return config


@router.post("/heartbeat", status_code=204)
async def heartbeat(
    # Loose by design — the installer/agent script (stage 6/7) isn't finalized yet, and this
    # endpoint's only job is to store whatever the agent reports, not validate a fleet-wide
    # schema no device exists yet to have gotten wrong.
    body: dict[str, Any],
    device: RouterDevice = Depends(require_device),
    container: AppContainer = Depends(get_container),
) -> None:
    if not await _rate_limit_ok(container, f"agent:hb:{device.id}"):
        raise HTTPException(429, "too many requests")
    async with container.uow() as uow:
        fresh = await uow.router_devices.get(device.id)
        if fresh is None:
            return
        fresh.last_seen_at = dt.datetime.now(dt.UTC)
        xray_running = body.get("xray_running")
        fresh.status = (
            RouterDeviceStatus.OFFLINE
            if xray_running is False
            else RouterDeviceStatus.ONLINE
        )
        if body.get("xray_version") is not None:
            fresh.xray_version = str(body["xray_version"])[:32]
        if body.get("active_outbound") is not None:
            fresh.active_outbound = str(body["active_outbound"])[:64]
        if body.get("external_ip") is not None:
            fresh.external_ip = str(body["external_ip"])[:45]
        last_error = body.get("last_error")
        fresh.last_error = str(last_error)[:_LAST_ERROR_MAX] if last_error else None
        await uow.commit()


@router.post("/install-report", status_code=204)
async def install_report(
    body: dict[str, Any],
    device: RouterDevice = Depends(require_device),
    container: AppContainer = Depends(get_container),
) -> None:
    """Preflight/install result from the installer script (stage 7) — stored as-is so an admin
    can see what a technician's install actually did without visiting the router."""
    async with container.uow() as uow:
        fresh = await uow.router_devices.get(device.id)
        if fresh is None:
            return
        fresh.install_report = body
        if fresh.status is RouterDeviceStatus.PENDING:
            fresh.status = RouterDeviceStatus.ONLINE
        fresh.last_seen_at = dt.datetime.now(dt.UTC)
        await uow.commit()
