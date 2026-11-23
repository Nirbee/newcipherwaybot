"""DevVersion API for the Windows app (``/api/app/v1/telemetry``) — diagnostics the user opted in
to (consent shown by the app; its version travels in every report).

Same credential as the rest of ``/api/app/v1``: ``X-Subscription-Token`` (see app_api.py); the
subscription must be live (403 ``subscription_inactive``), paid (403 ``trial_not_allowed``) and,
when the admin keeps an allow-list, on it (403 ``not_allowed``) — /config answers
``enabled: false`` with that ``reason`` instead. DELETE is always allowed: wiping one's own
reports needs nothing but the token. The kill switch (``DEVVERSION_TELEMETRY_ENABLED``) answers
``enabled: false`` in /config and 404 ``telemetry_disabled`` to uploads.

Disk guards on upload, all in Redis and FAIL-CLOSED (Redis down → 503, unlike the other app
limiters): 6/hour and 60/day per subscription, 100/day per IP (keyed by an HMAC of the IP),
20 MB/day per account and 1 GB/day for everyone (bytes after gunzip) → 429; at most 512 KB on
the wire (also BodyLimitMiddleware) and 512 KB after gunzip (the inflater never produces more —
no zip bomb); the newest 500 reports per subscription are kept; past 3 GB of table the
retention task raises a «storage full» flag and uploads get 503 ``storage_full``. The body
is read only after the token, the gates and the count limits passed; gunzip, the checks and
the digest run in a thread (``application/services/devversion.py``). Provider and region come
from the request IP via an offline database (``infrastructure/services/geoip.py``); the IP
itself is neither stored nor logged here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from src.application.services.devversion import (
    BYTES_PER_USER_DAY,
    BYTES_TOTAL_DAY,
    MAX_REPORT_BYTES,
    MAX_REPORTS_PER_SUBSCRIPTION,
    UPLOADS_PER_DAY,
    UPLOADS_PER_HOUR,
    UPLOADS_PER_IP_DAY,
    DevVersionConfig,
    ReportError,
    prepare_upload,
)
from src.application.services.lan import subscription_lan_active
from src.core.enums import SubscriptionStatus
from src.core.logging import get_logger
from src.infrastructure.database.models.devversion import DevversionReport
from src.infrastructure.di import AppContainer
from src.infrastructure.services import geoip
from src.infrastructure.services.devversion import STORAGE_FULL_KEY, load_config
from src.web.deps import get_container
from src.web.routes.app_api import AppIdentity, app_identity, ip_key
from src.web.routes.cabinet_auth import _client_ip

log = get_logger(__name__)

router = APIRouter(prefix="/api/app/v1/telemetry", tags=["app"])

_DAY = 86400


async def _refusal(
    container: AppContainer, ident: AppIdentity, cfg: DevVersionConfig
) -> str | None:
    """403 for a lapsed subscription; otherwise why it may not send (trial / not listed)."""
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(ident.subscription_id)
    if sub is None or not subscription_lan_active(sub.is_usable, sub.expire_at):
        raise HTTPException(403, "subscription_inactive")
    trial = bool(sub.is_trial) or sub.status is SubscriptionStatus.TRIAL
    return cfg.refusal(sub.id, trial=trial)


def _unavailable(exc: Exception) -> HTTPException:
    log.warning("devversion: redis unavailable", error=type(exc).__name__)
    return HTTPException(503, "temporarily_unavailable")


# Give bytes back to a quota: only to a counter that still exists (an expired window is not
# recreated without a TTL) and never below zero; DECRBY keeps the key's TTL.
_GIVE_BACK_LUA = (
    "if redis.call('exists', KEYS[1]) == 0 then return 0 end "
    "local n = redis.call('decrby', KEYS[1], ARGV[1]) "
    "if n < 0 then redis.call('set', KEYS[1], 0, 'KEEPTTL') n = 0 end "
    "return n"
)


async def _add(container: AppContainer, key: str, amount: int, window: int) -> int:
    """INCRBY in a fixed window. INCRBY and EXPIRE NX go in one MULTI on EVERY call, so a counter
    can never be left without a TTL (that would lock the quota for good) — same as the admin
    key limiter. Fail-CLOSED: no Redis → 503 (the disk guards rely on it)."""
    try:
        pipe = container.redis.pipeline(transaction=True)
        pipe.incrby(key, amount)
        pipe.expire(key, window, nx=True)
        n, _ = await pipe.execute()
    except Exception as exc:
        raise _unavailable(exc) from exc
    return int(n)


async def _take_back(container: AppContainer, key: str, amount: int) -> None:
    try:
        await cast("Awaitable[Any]", container.redis.eval(_GIVE_BACK_LUA, 1, key, str(amount)))
    except Exception as exc:
        raise _unavailable(exc) from exc


async def _peek(container: AppContainer, key: str) -> int:
    try:
        return int(await container.redis.get(key) or 0)
    except Exception as exc:
        raise _unavailable(exc) from exc


@router.get("/config")
async def telemetry_config(
    ident: AppIdentity = Depends(app_identity("telemetry")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    cfg = await load_config(container)
    return cfg.public(await _refusal(container, ident, cfg))


async def _read_body(request: Request) -> bytes:
    """The raw body, counted while it streams in — 413 past the cap, whatever the headers say."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REPORT_BYTES:
            raise HTTPException(413, "report_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", status_code=202)
async def telemetry_upload(
    request: Request,
    ident: AppIdentity = Depends(app_identity("telemetry")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    cfg = await load_config(container)
    if not cfg.enabled:
        raise HTTPException(404, "telemetry_disabled")
    refusal = await _refusal(container, ident, cfg)
    if refusal is not None:
        raise HTTPException(403, refusal)
    if await _peek(container, STORAGE_FULL_KEY):  # the table hit its size ceiling
        raise HTTPException(503, "storage_full")
    sub_id, user_id = ident.subscription_id, ident.user.id
    ip = ip_key(container, _client_ip(request))
    too_many = HTTPException(429, "too many attempts — try again later")
    if await _add(container, f"dv:hour:{sub_id}", 1, 3600) > UPLOADS_PER_HOUR:
        raise too_many
    if await _add(container, f"dv:day:{sub_id}", 1, _DAY) > UPLOADS_PER_DAY:
        raise too_many
    if ip and await _add(container, f"dv:ip:{ip}", 1, _DAY) > UPLOADS_PER_IP_DAY:
        raise too_many
    if await _peek(container, "dv:bytes:all") >= BYTES_TOTAL_DAY:
        raise too_many
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "unsupported_media_type")
    raw = await _read_body(request)
    encoding = request.headers.get("content-encoding", "")
    try:
        report, digest = await asyncio.to_thread(prepare_upload, raw, encoding)
    except ReportError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    # Bytes quotas, counted by the real (inflated) size; a refused report gives them back.
    user_key = f"dv:bytes:user:{user_id}"
    if await _add(container, user_key, report.size, _DAY) > BYTES_PER_USER_DAY:
        await _take_back(container, user_key, report.size)
        raise too_many
    if await _add(container, "dv:bytes:all", report.size, _DAY) > BYTES_TOTAL_DAY:
        await _take_back(container, "dv:bytes:all", report.size)
        await _take_back(container, user_key, report.size)
        raise too_many
    geo = geoip.lookup(container.settings.web.geoip_dir, _client_ip(request))
    async with container.uow() as uow:
        row = await uow.devversion_reports.add(
            DevversionReport(
                subscription_id=sub_id,
                install_id=report.install_id,
                app_version=report.app_version,
                consent_version=report.consent_version,
                schema=report.schema,
                asn=geo.asn,
                as_org=geo.as_org,
                country=geo.country,
                region=geo.region,
                city=geo.city,
                size=report.size,
                payload=report.payload,
                digest=digest,
            )
        )
        report_id = row.id
        trimmed = await uow.devversion_reports.trim_subscription(
            sub_id, MAX_REPORTS_PER_SUBSCRIPTION
        )
        await uow.commit()
    log.info(
        "devversion report stored",
        report_id=report_id,
        subscription_id=sub_id,
        size=report.size,
        trimmed=trimmed,
    )
    return {"ok": True}


@router.delete("")
async def telemetry_delete(
    ident: AppIdentity = Depends(app_identity("telemetry")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Every report of THIS subscription (the token's), sent from any install."""
    async with container.uow() as uow:
        deleted = await uow.devversion_reports.delete_for_subscription(ident.subscription_id)
        await uow.commit()
    log.info("devversion reports deleted by the app", subscription_id=ident.subscription_id)
    return {"deleted": deleted}
