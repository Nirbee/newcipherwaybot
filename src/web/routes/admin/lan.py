"""Admin: LAN Play (Settings → «LAN для игр») — Headscale connection check + rooms list.

The settings themselves (switch, server, API key, limits) are ordinary registry params of the
``lan`` category, edited on the settings screen; the API key is a SECRET param (masked in
every listing, never sent back). Room codes are not shown here — a code is a join credential.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from src.infrastructure.di import AppContainer
from src.infrastructure.services.lan import check_connection
from src.web.deps import get_container
from src.web.routes.admin._common import audit
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/settings/lan")


@router.get("/rooms")
async def lan_rooms(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        rows = await uow.lan_rooms.with_member_counts()
    items = [
        {
            "id": room.id,
            "name": room.name,
            "owner_subscription_id": room.owner_subscription_id,
            "members": members,
            "created_at": room.created_at.isoformat() if room.created_at else None,
        }
        for room, members in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/check")
async def lan_check(
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Call the Headscale API with the SAVED server + key: health, users, nodes and a trial
    PUT of the current DB policy (catches ``policy.mode: file``). Short reasons only.
    Audited: the trial PUT writes to the Headscale server."""
    result = await check_connection(container)
    async with container.uow() as uow:
        await audit(uow, identity, "lan.check", None, ok=result["ok"], error=result.get("error"))
        await uow.commit()
    return result
