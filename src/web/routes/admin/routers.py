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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from src.application.dto.pricing import PurchaseRequest
from src.application.services import router_onboarding as onboarding
from src.core.enums import (
    PaymentGatewayType,
    PlanCategory,
    RouterDeviceMode,
    RouterDeviceStatus,
    TransactionStatus,
)
from src.core.exceptions import DomainError, RemnawaveError
from src.core.logging import get_logger
from src.infrastructure.database.models.router_device import RouterDevice
from src.infrastructure.database.models.user import User
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
    if user.username:
        return f"@{user.username}"
    if user.telegram_id:
        return f"id{user.telegram_id}"
    return user.email or None


async def _bot_username(container: AppContainer, uow: UnitOfWork) -> str:
    return str(await container.bot_config.value(uow, "BOT_USERNAME") or "").lstrip("@")


def _sub_summary(sub: Any) -> dict[str, Any] | None:
    if sub is None:
        return None
    return {
        "id": sub.id,
        "status": sub.status.value,
        "is_trial": sub.is_trial,
        "expire_at": iso(sub.expire_at),
        "plan_name": (sub.plan_snapshot or {}).get("name"),
        "device_limit": sub.device_limit,
    }


def _row(device: RouterDevice, sub_label: str | None) -> dict[str, Any]:
    return {
        "id": device.id,
        "label": device.label,
        "subscription_id": device.subscription_id,
        "subscription_label": sub_label,
        "awaiting_claim": device.claim_code is not None,
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
    """No customer given -> a QR-onboarding router: a placeholder customer on a router trial,
    claimed when the real customer scans the QR. ``user_id`` -> an existing customer (found by
    @username). ``subscription_id`` -> attach to a known subscription as-is."""

    label: str = Field(..., min_length=1, max_length=128)
    user_id: int | None = None
    subscription_id: int | None = None
    mode: RouterDeviceMode = RouterDeviceMode.AUTO
    primary_host_uuid: str | None = None
    backup_host_uuid: str | None = None
    note: str | None = Field(None, max_length=512)

    @model_validator(mode="after")
    def _validate(self) -> RouterCreateIn:
        if self.mode is RouterDeviceMode.FORCE and not self.primary_host_uuid:
            raise ValueError("FORCE mode requires primary_host_uuid")
        if self.user_id is not None and self.subscription_id is not None:
            raise ValueError("pass user_id or subscription_id, not both")
        return self


async def _trial_days(container: AppContainer, uow: UnitOfWork) -> int:
    return int(await container.bot_config.value(uow, "ROUTER_TRIAL_DAYS") or 3)


@router.post("")
async def create_device(
    body: RouterCreateIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Returns the plain bearer token ONCE — only its sha256 is ever stored."""
    notify: tuple[int, str] | None = None
    claim_code: str | None = None
    async with container.uow() as uow:
        label = body.label.strip()
        try:
            if body.subscription_id is not None:
                sub = await uow.subscriptions.get(body.subscription_id)
                if sub is None:
                    raise HTTPException(404, "subscription not found")
            elif body.user_id is not None:
                customer = await uow.users.get(body.user_id)
                if customer is None:
                    raise HTTPException(404, "user not found")
                current = (
                    await uow.subscriptions.get(customer.current_subscription_id)
                    if customer.current_subscription_id
                    else None
                )
                if current is not None and current.status.is_usable:
                    sub = current
                else:
                    sub = await onboarding.start_trial(
                        uow,
                        container.subscriptions,
                        user=customer,
                        days=await _trial_days(container, uow),
                    )
                if customer.telegram_id:
                    text = onboarding.attached_text(
                        label, sub, await _bot_username(container, uow)
                    )
                    notify = (customer.telegram_id, text)
            else:
                placeholder = await onboarding.new_placeholder_customer(uow, label=label)
                sub = await onboarding.start_trial(
                    uow,
                    container.subscriptions,
                    user=placeholder,
                    days=await _trial_days(container, uow),
                )
                claim_code = onboarding.new_claim_code()
        except onboarding.RouterOnboardingError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RemnawaveError as exc:
            raise HTTPException(502, f"panel error: {exc}") from exc

        if body.mode is RouterDeviceMode.AUTO:
            eligible = await _eligible_host_uuids(uow)
            primary, backup = await _auto_assign_hosts(container, uow, eligible)
        else:
            primary, backup = body.primary_host_uuid, body.backup_host_uuid

        token = secrets.token_urlsafe(32)
        device = RouterDevice(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            label=label,
            subscription_id=sub.id,
            mode=body.mode,
            primary_host_uuid=primary,
            backup_host_uuid=backup,
            note=(body.note or "").strip() or None,
            claim_code=claim_code,
        )
        await uow.router_devices.add(device)
        await onboarding.tag_router_subscription(container.remnawave, uow, sub)
        await audit(
            uow,
            identity,
            "routers.create",
            f"router:{device.id}",
            subscription_id=sub.id,
            mode=body.mode.value,
            qr=claim_code is not None,
        )
        bot_username = await _bot_username(container, uow)
        summary = _sub_summary(sub)
        await uow.commit()

    if notify is not None:
        await container.notifier.notify_user(*notify)
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
        "claim_url": (
            onboarding.claim_url(bot_username, claim_code) if claim_code and bot_username else None
        ),
        "subscription": summary,
    }


# --- technician helpers (under /routers so a routers-only staff account can use them) --------


@router.get("/customers")
async def find_customers(
    q: str = "", container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Exact @username lookup — deliberately not a browsable user list: a routers-only
    technician should find the customer in front of them, not page through the customer base."""
    name = q.strip().lstrip("@").lower()
    if len(name) < 3:
        return {"items": []}
    async with container.uow() as uow:
        users = (
            await uow.session.scalars(
                select(User).where(func.lower(User.username) == name).limit(5)
            )
        ).all()
        items = []
        for u in users:
            sub = (
                await uow.subscriptions.get(u.current_subscription_id)
                if u.current_subscription_id
                else None
            )
            items.append(
                {
                    "id": u.id,
                    "username": u.username,
                    "name": u.first_name,
                    "subscription": _sub_summary(sub),
                }
            )
    return {"items": items}


@router.get("/plans")
async def list_router_plans(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        plans = await onboarding.router_plans(uow)
        items = [
            {
                "id": p.id,
                "name": p.name,
                "device_limit": p.device_limit,
                "durations": [
                    {"days": d.days, "price_minor": onboarding.duration_rub(p, d.days)}
                    for d in sorted(p.durations, key=lambda d: d.days)
                ],
            }
            for p in plans
        ]
    return {"items": items}


class RouterPaidIn(BaseModel):
    plan_id: int
    days: int = Field(..., ge=1, le=3650)


@router.post("/{device_id}/paid")
async def mark_paid(
    device_id: int,
    body: RouterPaidIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """«Клиент оплатил на месте»: a real (cash) sale through the normal purchase pipeline — it
    lands in sales/revenue like any payment and renews/switches the router's subscription
    exactly as a bot purchase would."""
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        sub = await uow.subscriptions.get(device.subscription_id)
        owner = await uow.users.get(sub.user_id) if sub is not None else None
        if owner is None:
            raise HTTPException(400, "router has no customer")
        plan = await uow.plans.get_with_durations(body.plan_id)
        if plan is None or plan.category is not PlanCategory.ROUTER or not plan.is_active:
            raise HTTPException(404, "router plan not found")
        if onboarding.duration_rub(plan, body.days) is None:
            raise HTTPException(400, "this plan has no such duration")
        try:
            ptype, sub_id = await container.purchase.resolve_purchase_type(uow, owner.id, plan.id)
            req = PurchaseRequest(
                user_id=owner.id,
                plan_id=plan.id,
                duration_days=body.days,
                currency=owner.currency,
                purchase_type=ptype,
                subscription_id=sub_id,
            )
            txn, quote = await container.purchase.start(uow, req)
            txn.gateway_type = PaymentGatewayType.MANUAL
            txn.gateway_display_name = onboarding.CASH_DISPLAY_NAME
            if txn.status is TransactionStatus.PENDING:
                await container.payments.process(
                    uow, payment_id=txn.payment_id, status=TransactionStatus.COMPLETED
                )
        except RemnawaveError as exc:
            raise HTTPException(502, f"panel error: {exc}") from exc
        except DomainError as exc:
            raise HTTPException(400, str(exc)) from exc
        await uow.flush()
        # One subscription per customer: the purchase acted on the CURRENT subscription (the
        # one the router rides on); keep the router pointed at it.
        live_id = owner.current_subscription_id or device.subscription_id
        device.subscription_id = live_id
        live = await uow.subscriptions.get(live_id)
        await audit(
            uow,
            identity,
            "routers.paid",
            f"router:{device.id}",
            plan_id=plan.id,
            days=body.days,
            amount_minor=quote.final.amount_minor,
        )
        summary = _sub_summary(live)
        notify = (
            (owner.telegram_id, onboarding.paid_text(plan.name, live))
            if owner.telegram_id
            else None
        )
        await uow.commit()
    if notify is not None:
        await container.notifier.notify_user(*notify)
    return {"ok": True, "subscription": summary, "amount_minor": quote.final.amount_minor}


@router.get("/{device_id}")
async def get_device(
    device_id: int, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        label = await _subscription_label(uow, device.subscription_id)
        summary = _sub_summary(await uow.subscriptions.get(device.subscription_id))
        bot_username = await _bot_username(container, uow)
    try:
        hosts = await container.remnawave_client.get_hosts()
    except Exception:
        hosts = []
    detail = _row(device, label)
    detail["install_report"] = device.install_report
    detail["diagnostics"] = device.diagnostics
    detail["subscription"] = summary
    detail["claim_url"] = (
        onboarding.claim_url(bot_username, device.claim_code)
        if device.claim_code and bot_username
        else None
    )
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
