"""Admin: «📊 DevVersion» — settings, summaries, reports, export and deletion of the diagnostics
the Windows app uploads (``/api/app/v1/telemetry``, see app_telemetry.py).

Who sees what: the list, the summaries and the settings — any staff with the screen. The RAW
payload of a report — OWNER / DEV, and the owner-issued read-only AI key (the owner analyses the
data with it); other staff get ``"payload": "***"``. The export — OWNER / DEV only (closed to
keys: ``KEY_BLOCKED_PATHS``). Viewing a report and exporting are audited. Settings (switch,
interval, flags, allow-list) — OWNER / DEV only; deleting a subscription's reports — staff.
Key answers go through the key filter (e-mails / phones inside payloads are masked there). The
subscription is shown by id plus the owner's @username / Telegram id — never e-mail or phone.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from src.application.services.devversion import (
    CONSENT_VERSION,
    MAX_REPORT_BYTES,
    RETENTION_DAYS,
    SUMMARY_CELLS,
    DigestRow,
    check_interval,
    clean_allowed,
    clean_flags,
    digest_cells,
    summarize,
)
from src.core.enums import Role
from src.infrastructure.database.dao.devversion import ReportFilter
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.di import AppContainer
from src.infrastructure.services import geoip
from src.infrastructure.services.devversion import load_config
from src.web.deps import get_container
from src.web.routes.admin._common import audit, iso
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/devversion")

_SUMMARY_REPORTS = 1000  # newest reports a summary reads (their digests, not payloads)
_EXPORT_REPORTS = 2000
_EXPORT_BYTES = 128 * 1024 * 1024  # payload bytes per export file (streamed, not buffered)
_RAW_ROLES = (Role.OWNER, Role.DEV)
_HIDDEN = "***"
_ATTRIBUTION = "IP Geolocation by DB-IP (https://db-ip.com), CC BY 4.0"


def _sees_raw(identity: AdminIdentity) -> bool:
    """Raw payloads: OWNER / DEV sessions and the owner's read-only AI key."""
    return identity.api_key_id is not None or identity.role in _RAW_ROLES


def _require_raw_role(identity: AdminIdentity) -> None:
    if identity.api_key_id is not None or identity.role not in _RAW_ROLES:
        raise HTTPException(403, "только владелец или разработчик")


def _filter(
    asn: int | None = Query(None, ge=0, description="0 — провайдер не определён"),
    app_version: str | None = Query(None, max_length=32),
    subscription_id: int | None = Query(None, ge=1),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
) -> ReportFilter:
    """Query filters; dates are whole UTC days, ``date_to`` inclusive."""

    def start(day: dt.date) -> dt.datetime:
        return dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)

    return ReportFilter(
        asn=asn,
        app_version=app_version or None,
        subscription_id=subscription_id,
        since=start(date_from) if date_from else None,
        until=start(date_to) + dt.timedelta(days=1) if date_to else None,
    )


async def _labels(uow: UnitOfWork, sub_ids: Iterable[int]) -> dict[int, str | None]:
    """subscription id → owner's @username / id<telegram> (no e-mail, no phone)."""
    ids = set(sub_ids)
    if not ids:
        return {}
    rows = await uow.session.execute(
        select(Subscription.id, User.username, User.telegram_id)
        .join(User, User.id == Subscription.user_id)
        .where(Subscription.id.in_(ids))
    )
    out: dict[int, str | None] = {}
    for sub_id, username, telegram_id in rows.all():
        out[int(sub_id)] = (
            f"@{username}" if username else f"id{telegram_id}" if telegram_id else None
        )
    return out


def _row(r: Any, labels: dict[int, str | None], *, place: bool = True) -> dict[str, Any]:
    """List view of a report — an ORM row or a plain row mapping (the export stream). Without
    ``place`` (staff that doesn't see raw data) region and city are left out — country and
    provider stay."""
    get = r.__getitem__ if isinstance(r, Mapping) else r.__getattribute__
    digest = get("digest") or {}
    sub_id = get("subscription_id")
    return {
        "id": get("id"),
        "subscription_id": sub_id,
        "subscription_label": labels.get(sub_id),
        "install_id": get("install_id"),
        "received_at": iso(get("received_at")),
        "app_version": get("app_version"),
        "consent_version": get("consent_version"),
        "schema": get("schema"),
        "asn": get("asn"),
        "as_org": get("as_org"),
        "country": get("country"),
        "region": get("region") if place else None,
        "city": get("city") if place else None,
        "mode": digest.get("mode"),
        "size": get("size"),
        "counts": digest.get("counts") or {},
    }


# --- settings -------------------------------------------------------------------------------
async def _settings_view(container: AppContainer, identity: AdminIdentity) -> dict[str, Any]:
    cfg = await load_config(container)
    return {
        "enabled": cfg.enabled,
        "upload_interval_s": cfg.upload_interval_s,
        "flags": cfg.flags,
        "allowed_subscriptions": sorted(cfg.allowed),
        "consent_version": CONSENT_VERSION,
        "max_report_bytes": MAX_REPORT_BYTES,
        "retention_days": RETENTION_DAYS,
        "geoip": geoip.status(container.settings.web.geoip_dir),
        # what THIS viewer may do (the screen hides the rest)
        "can_edit": identity.api_key_id is None and identity.role in _RAW_ROLES,
        "can_see_raw": _sees_raw(identity),
        "can_export": identity.api_key_id is None and identity.role in _RAW_ROLES,
    }


@router.get("/settings")
async def devversion_settings(
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return await _settings_view(container, identity)


class SettingsIn(BaseModel):
    enabled: bool | None = None
    upload_interval_s: int | None = None
    flags: dict[str, Any] | str | None = None
    allowed_subscriptions: list[int] | str | None = None


@router.put("/settings")
async def devversion_settings_save(
    body: SettingsIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    _require_raw_role(identity)
    changes: dict[str, Any] = {}
    try:
        if body.enabled is not None:
            changes["DEVVERSION_TELEMETRY_ENABLED"] = body.enabled
        if body.upload_interval_s is not None:
            changes["DEVVERSION_UPLOAD_INTERVAL"] = check_interval(body.upload_interval_s)
        if body.flags is not None:
            changes["DEVVERSION_FLAGS"] = clean_flags(body.flags)
        if body.allowed_subscriptions is not None:
            changes["DEVVERSION_ALLOWED_SUBSCRIPTIONS"] = clean_allowed(body.allowed_subscriptions)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not changes:
        raise HTTPException(400, "no changes")
    async with container.uow() as uow:
        written = await container.bot_config.set_values(uow, changes)
        await audit(uow, identity, "devversion.settings", None, keys=written)
        await uow.commit()
    return await _settings_view(container, identity)


# --- reports ------------------------------------------------------------------------------
@router.get("/facets")
async def devversion_facets(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        return await uow.devversion_reports.facets()


@router.get("/reports")
async def devversion_reports(
    flt: ReportFilter = Depends(_filter),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    place = _sees_raw(identity)
    async with container.uow() as uow:
        rows, total = await uow.devversion_reports.page(flt, limit=limit, offset=offset)
        labels = await _labels(uow, (r.subscription_id for r in rows))
    return {
        "items": [_row(r, labels, place=place) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/reports/{report_id}")
async def devversion_report(
    report_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """One report; the raw payload only for OWNER / DEV and the AI key (audited either way)."""
    raw = _sees_raw(identity)
    async with container.uow() as uow:
        row = await uow.devversion_reports.get(report_id)
        if row is None:
            raise HTTPException(404, "report not found")
        labels = await _labels(uow, [row.subscription_id])
        out = {**_row(row, labels, place=raw), "payload": row.payload if raw else _HIDDEN}
        await audit(
            uow,
            identity,
            "devversion.view",
            f"report:{report_id}",
            subscription_id=row.subscription_id,
            payload_shown=raw,
        )
        await uow.commit()
    return out


@router.get("/summary")
async def devversion_summary(
    flt: ReportFilter = Depends(_filter),
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Provider x node/protocol, direct failures by app/domain, game tests — over the newest
    ``_SUMMARY_REPORTS`` matching reports. Digests are read through a cursor and the read stops
    at the report / cell ceiling (``truncated``); the numbers are crunched in a thread. One
    subscription's summary is OWNER / DEV / AI-key only (it is that customer's data), audited."""
    if flt.subscription_id is not None:
        if not _sees_raw(identity):
            raise HTTPException(403, "сводка по одной подписке — только владелец или разработчик")
        async with container.uow() as uow:
            await audit(
                uow,
                identity,
                "devversion.summary",
                f"subscription:{flt.subscription_id}",
            )
            await uow.commit()
    digests: list[DigestRow] = []
    cells = 0
    truncated = False
    async with (
        container.uow() as uow,
        aclosing(uow.devversion_reports.stream_digests(flt, limit=_SUMMARY_REPORTS + 1)) as rows,
    ):
        async for i, s, a, o, d in rows:
            size = digest_cells(d)
            if len(digests) >= _SUMMARY_REPORTS or cells + size > SUMMARY_CELLS:
                truncated = True
                break
            cells += size
            digests.append(DigestRow(report_id=i, subscription_id=s, asn=a, as_org=o, digest=d))
    summary = await asyncio.to_thread(summarize, digests)
    return {**summary, "truncated": summary["truncated"] or truncated}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


@router.get("/export")
async def devversion_export(
    flt: ReportFilter = Depends(_filter),
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    """Matching reports with their payloads as one JSON file, newest first, STREAMED from a
    server-side cursor (never the whole set in memory), within ``_EXPORT_REPORTS`` reports /
    ``_EXPORT_BYTES`` of payload (``truncated`` at the end says it was cut). OWNER / DEV."""
    _require_raw_role(identity)
    async with container.uow() as uow:
        labels = await _labels(uow, await uow.devversion_reports.subscription_ids(flt))
        await audit(
            uow,
            identity,
            "devversion.export",
            None,
            asn=flt.asn,
            app_version=flt.app_version,
            subscription_id=flt.subscription_id,
            since=iso(flt.since),
            until=iso(flt.until),
        )
        await uow.commit()
    stamp = dt.datetime.now(dt.UTC)

    async def lines() -> AsyncIterator[str]:
        yield f'{{"exported_at":{_json(stamp.isoformat())},"attribution":{_json(_ATTRIBUTION)},'
        yield '"reports":[\n'
        sent = spent = 0
        truncated = False
        async with (
            container.uow() as uow,
            aclosing(uow.devversion_reports.stream(flt, limit=_EXPORT_REPORTS + 1)) as rows,
        ):
            async for row in rows:
                spent += int(row["size"] or 0)
                if sent >= _EXPORT_REPORTS or (sent and spent > _EXPORT_BYTES):
                    truncated = True
                    break
                yield ("" if sent == 0 else ",\n") + _json(
                    {**_row(row, labels), "payload": row["payload"]}
                )
                sent += 1
        yield f'\n],"truncated":{_json(truncated)}}}\n'

    name = f"devversion-{stamp:%Y%m%d-%H%M}.json"
    return StreamingResponse(
        lines(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.delete("/subscriptions/{subscription_id}/reports")
async def devversion_delete_subscription(
    subscription_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.uow() as uow:
        deleted = await uow.devversion_reports.delete_for_subscription(subscription_id)
        await audit(
            uow,
            identity,
            "devversion.delete",
            f"subscription:{subscription_id}",
            deleted=deleted,
        )
        await uow.commit()
    return {"deleted": deleted}
