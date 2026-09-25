"""Admin: router devices — the XKeen/Xray control-plane fleet (screen: Routers).

A RouterDevice is deliberately NOT its own Remnawave panel user — it attaches to an existing
Subscription (see the model's docstring). This module only manages the device row itself
(token, host assignment, status) plus a small admin-curated allowlist of which panel hosts are
even eligible for AUTO host assignment (opt-in: empty by default, so nothing gets auto-assigned
until an admin explicitly adds hosts — see the "config/eligible-hosts" routes).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from src.core.enums import RouterDeviceMode, RouterDeviceStatus
from src.core.logging import get_logger
from src.infrastructure.database.models.router_device import RouterDevice
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import OkOut, audit, iso
from src.web.routes.admin.deps import AdminIdentity, require_admin

log = get_logger(__name__)
router = APIRouter(prefix="/routers")

# A single bot_config row (not a registered registry key — this doesn't belong on the generic
# settings screen, only this one purpose-built UI): a JSON list of eligible panel host uuids.
_ELIGIBLE_HOSTS_KEY = "ROUTER_ELIGIBLE_HOSTS"
_STALE_AFTER = dt.timedelta(minutes=15)


async def _eligible_host_uuids(uow: UnitOfWork) -> set[str]:
    row = await uow.bot_config.find_one(key=_ELIGIBLE_HOSTS_KEY)
    value = row.value if row is not None else []
    return {str(v) for v in (value or [])}


def _is_online(device: RouterDevice) -> bool:
    if device.status is not RouterDeviceStatus.ONLINE or device.last_seen_at is None:
        return False
    return dt.datetime.now(dt.UTC) - device.last_seen_at < _STALE_AFTER


async def _subscription_label(uow: UnitOfWork, subscription_id: int) -> str | None:
    sub = await uow.subscriptions.get(subscription_id)
    if sub is None:
        return None
    user = await uow.users.get(sub.user_id)
    if user is None:
        return None
    return f"@{user.username}" if user.username else f"id{user.telegram_id}"


def _row(device: RouterDevice, sub_label: str | None) -> dict[str, Any]:
    return {
        "id": device.id,
        "label": device.label,
        "subscription_id": device.subscription_id,
        "subscription_label": sub_label,
        "mode": device.mode.value,
        "status": device.status.value,
        "is_online": _is_online(device),
        "primary_host_uuid": device.primary_host_uuid,
        "backup_host_uuid": device.backup_host_uuid,
        "config_etag": device.config_etag,
        "last_seen_at": iso(device.last_seen_at),
        "xray_version": device.xray_version,
        "active_outbound": device.active_outbound,
        "external_ip": device.external_ip,
        "last_error": device.last_error,
        "note": device.note,
        "created_at": iso(device.created_at),
    }


async def _auto_assign_hosts(
    container: AppContainer, uow: UnitOfWork, eligible: set[str]
) -> tuple[str | None, str | None]:
    """Least-connections primary + optional backup over the eligible allowlist.

    (None, None) — an empty allowlist, an unreachable panel, or no matching hosts — is not an
    error here: the caller (create_device) accepts it and the device just gets a freedom-only
    config until an admin adds eligible hosts, rather than blocking device/token creation on a
    technician standing at a customer's router waiting for it.
    """
    if not eligible:
        return None, None
    try:
        hosts = await container.remnawave_client.get_hosts()
    except Exception as exc:
        log.warning("router auto-assign: get_hosts failed", error=str(exc))
        return None, None
    candidates = [
        h for h in hosts if h.uuid in eligible and not h.is_disabled and h.protocol == "vless"
    ]
    if not candidates:
        return None, None
    counts = await uow.router_devices.host_assignment_counts([h.uuid for h in candidates])
    ranked = sorted(candidates, key=lambda h: (counts.get(h.uuid, 0), h.uuid))
    primary = ranked[0].uuid
    backup = ranked[1].uuid if len(ranked) > 1 else None
    return primary, backup


async def _tag_subscription_as_router(container: AppContainer, uow: UnitOfWork, sub: Any) -> None:
    """Best-effort: mark the shared subscription's panel user with tag=ROUTER, purely for staff
    visibility when browsing Remnawave's own UI. Never blocks device creation on panel
    availability — a failed tag write just means the panel user looks like any other."""
    panel_ref = sub.panel_ref
    if panel_ref is None:
        return
    user = await uow.users.get(sub.user_id)
    panel_ref = replace(panel_ref, telegram_id=user.telegram_id if user else None)
    spec = container.remnawave.build_spec(
        short_id=sub.short_id,
        telegram_id=user.telegram_id if user else None,
        expire_at=sub.expire_at or dt.datetime.now(dt.UTC),
        traffic_limit_bytes=sub.traffic_limit_bytes,
        device_limit=sub.device_limit,
        internal_squads=tuple(sub.internal_squads or ()),
        external_squad=sub.external_squad,
        tag="ROUTER",
    )
    try:
        await container.remnawave.apply(panel_ref, spec)
    except Exception as exc:
        log.warning("router device: failed to tag subscription's panel user", sub_id=sub.id, error=str(exc))


# --- eligible-hosts allowlist (registered BEFORE /{device_id} — literal routes must win) ------


@router.get("/config/eligible-hosts")
async def get_eligible_hosts(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        eligible = await _eligible_host_uuids(uow)
    try:
        hosts = await container.remnawave_client.get_hosts()
    except Exception as exc:
        raise HTTPException(502, "panel temporarily unavailable") from exc
    return {
        "items": [
            {
                "uuid": h.uuid,
                "remark": h.remark,
                "network": h.network,
                "security": h.security,
                "is_disabled": h.is_disabled,
                "eligible": h.uuid in eligible,
            }
            for h in hosts
            if h.protocol == "vless"
        ]
    }


class EligibleHostsIn(BaseModel):
    host_uuids: list[str]


@router.put("/config/eligible-hosts")
async def set_eligible_hosts(
    body: EligibleHostsIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        uuids = list(dict.fromkeys(body.host_uuids))  # de-dupe, keep order
        await uow.bot_config.upsert(_ELIGIBLE_HOSTS_KEY, uuids)
        await audit(uow, identity, "routers.eligible_hosts", None, count=len(uuids))
        await uow.commit()
    return OkOut()


# --- devices ------------------------------------------------------------------------------


@router.get("")
async def list_devices(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        devices = await uow.router_devices.list(limit=500)
        rows = [_row(d, await _subscription_label(uow, d.subscription_id)) for d in devices]
    return {"items": rows}


class RouterCreateIn(BaseModel):
    subscription_id: int
    label: str = Field(..., min_length=1, max_length=128)
    mode: RouterDeviceMode = RouterDeviceMode.AUTO
    primary_host_uuid: str | None = None
    backup_host_uuid: str | None = None
    note: str | None = Field(None, max_length=512)

    @model_validator(mode="after")
    def _force_needs_host(self) -> RouterCreateIn:
        if self.mode is RouterDeviceMode.FORCE and not self.primary_host_uuid:
            raise ValueError("FORCE mode requires primary_host_uuid")
        return self


@router.post("")
async def create_device(
    body: RouterCreateIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Returns the plain bearer token ONCE — only its sha256 is ever stored."""
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(body.subscription_id)
        if sub is None:
            raise HTTPException(404, "subscription not found")

        if body.mode is RouterDeviceMode.AUTO:
            eligible = await _eligible_host_uuids(uow)
            primary, backup = await _auto_assign_hosts(container, uow, eligible)
        else:
            primary, backup = body.primary_host_uuid, body.backup_host_uuid

        token = secrets.token_urlsafe(32)
        device = RouterDevice(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            label=body.label.strip(),
            subscription_id=sub.id,
            mode=body.mode,
            primary_host_uuid=primary,
            backup_host_uuid=backup,
            note=(body.note or "").strip() or None,
        )
        await uow.router_devices.add(device)
        await _tag_subscription_as_router(container, uow, sub)
        await audit(
            uow, identity, "routers.create", f"router:{device.id}",
            subscription_id=sub.id, mode=body.mode.value,
        )
        await uow.commit()

    warning = None
    if primary is None:
        warning = (
            "no eligible hosts configured — this device will get a freedom-only config"
            " until you add some under Routers -> eligible hosts"
        )
    return {
        "ok": True,
        "id": device.id,
        "token": token,
        "primary_host_uuid": primary,
        "backup_host_uuid": backup,
        "warning": warning,
    }


@router.get("/{device_id}")
async def get_device(
    device_id: int, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        label = await _subscription_label(uow, device.subscription_id)
    try:
        hosts = await container.remnawave_client.get_hosts()
    except Exception:
        hosts = []
    detail = _row(device, label)
    detail["install_report"] = device.install_report
    detail["diagnostics"] = device.diagnostics
    detail["available_hosts"] = [
        {"uuid": h.uuid, "remark": h.remark, "network": h.network, "is_disabled": h.is_disabled}
        for h in hosts
        if h.protocol == "vless"
    ]
    return detail


class RouterPatchIn(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=128)
    mode: RouterDeviceMode | None = None
    primary_host_uuid: str | None = None
    backup_host_uuid: str | None = None
    note: str | None = Field(None, max_length=512)


@router.patch("/{device_id}")
async def patch_device(
    device_id: int,
    body: RouterPatchIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        if body.label is not None:
            device.label = body.label.strip()
        if body.mode is not None:
            device.mode = body.mode
        if body.primary_host_uuid is not None:
            device.primary_host_uuid = body.primary_host_uuid or None
        if body.backup_host_uuid is not None:
            device.backup_host_uuid = body.backup_host_uuid or None
        if body.note is not None:
            device.note = body.note.strip() or None
        await audit(
            uow, identity, "routers.patch", f"router:{device.id}",
            **body.model_dump(exclude_none=True),
        )
        await uow.commit()
    return OkOut()


@router.post("/{device_id}/rotate")
async def rotate_token(
    device_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Issues a fresh token; the old one stops working immediately (its hash is gone)."""
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        token = secrets.token_urlsafe(32)
        device.token_hash = hashlib.sha256(token.encode()).hexdigest()
        await audit(uow, identity, "routers.rotate", f"router:{device.id}")
        await uow.commit()
    return {"ok": True, "token": token}


@router.post("/{device_id}/revoke")
async def revoke_device(
    device_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """Stops the device authenticating; the shared subscription (and any other device on it)
    is untouched. Does not rotate the subscription's own panel keys — see the plan's note on
    "отозвать и ротировать подписку" as a possible follow-up action."""
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        device.status = RouterDeviceStatus.REVOKED
        await audit(uow, identity, "routers.revoke", f"router:{device.id}")
        await uow.commit()
    return OkOut()


@router.delete("/{device_id}")
async def delete_device(
    device_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        await audit(uow, identity, "routers.delete", f"router:{device.id}")
        await uow.router_devices.delete(device)
        await uow.commit()
    return OkOut()
