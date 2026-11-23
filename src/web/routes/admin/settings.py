"""Admin: hot-reload bot settings (screen 13) — registry + overrides."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.application.services.bot_config import SECRET_MASK, BotConfigError, category_sections
from src.application.services.devversion import settings_guard as devversion_settings_guard
from src.application.services.panel_guard import SERVICE_SETTING
from src.application.services.panel_guard import settings_guard as service_accounts_guard
from src.core import config_registry as registry
from src.core.enums import Role
from src.infrastructure.di import AppContainer
from src.infrastructure.services.lan import settings_guard as lan_settings_guard
from src.web.deps import get_container
from src.web.routes.admin._common import audit
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/settings")

# LAN Play: where the bot sends the Headscale API key, and the key itself — owner only (a
# rogue server would receive the key and could hand customers' devices out).
_LAN_SERVER, _LAN_KEY = "LAN_LOGIN_SERVER", "LAN_HEADSCALE_API_KEY"


def _norm_server(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _require_owner_for_lan(identity: AdminIdentity, touches: bool) -> None:
    if touches and identity.role is not Role.OWNER:
        raise HTTPException(403, "сервер и API-ключ LAN меняет только владелец")


def _require_dev_for_devversion(identity: AdminIdentity, keys: Iterable[str]) -> None:
    """DevVersion switch / interval / flags / allow-list: OWNER or DEV only."""
    if any(k.startswith("DEVVERSION_") for k in keys) and identity.role not in (
        Role.OWNER,
        Role.DEV,
    ):
        raise HTTPException(403, "настройки DevVersion меняет только владелец или разработчик")


def _require_owner_for_service_accounts(identity: AdminIdentity, keys: Iterable[str]) -> None:
    """MultiScript's service accounts the bot must never touch: OWNER only (a wrong list could
    either expose the cascade to the bot's sweeps or block ordinary customers' renewals)."""
    if SERVICE_SETTING in keys and identity.role is not Role.OWNER:
        raise HTTPException(403, "список служебных учёток панели меняет только владелец")


@router.get("")
async def list_settings(
    q: str = Query("", max_length=64),
    category: str = Query("", max_length=16),
    lang: str = Query("ru", pattern="^(ru|en)$"),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.uow() as uow:
        rows = await container.bot_config.listing(uow, lang=lang)
    if category:
        rows = [r for r in rows if r["category"] == category]
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in r["key"].lower()
            or needle in r["name"].lower()
            or needle in r["description"].lower()
        ]
    return {
        "categories": category_sections(lang),
        "params": rows,
        "total": len(rows),
    }


class PatchIn(BaseModel):
    changes: dict[str, Any]


@router.patch("")
async def patch_settings(
    body: PatchIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    if not body.changes:
        raise HTTPException(400, "no changes")
    changes = dict(body.changes)
    _require_dev_for_devversion(identity, changes)
    _require_owner_for_service_accounts(identity, changes)
    async with container.uow() as uow:
        old_server = _norm_server(await container.bot_config.value(uow, _LAN_SERVER))
        _require_owner_for_lan(
            identity,
            (_LAN_SERVER in changes and _norm_server(changes[_LAN_SERVER]) != old_server)
            or (_LAN_KEY in changes and changes[_LAN_KEY] != SECRET_MASK),
        )
        try:
            # LAN: https-only server; a new server drops the key issued by the old one.
            reset = lan_settings_guard(changes, old_server)
            devversion_settings_guard(changes)  # JSON-object flags, bounded upload interval
            service_accounts_guard(changes)  # no pattern broad enough to catch customers
            written = await container.bot_config.set_values(uow, changes)
        except (BotConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if reset:
            await container.bot_config.reset(uow, reset)
            written += reset
        extra: dict[str, Any] = {}
        if _LAN_SERVER in written and changes[_LAN_SERVER] != old_server:
            extra = {"lan_server_old": old_server, "lan_server_new": changes[_LAN_SERVER]}
        await audit(uow, identity, "settings.patch", None, keys=written, **extra)
        await uow.commit()
    return {"ok": True, "applied": written}


class ResetIn(BaseModel):
    keys: list[str]


@router.post("/reset")
async def reset_settings(
    body: ResetIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    keys = list(dict.fromkeys(body.keys))
    _require_owner_for_lan(identity, _LAN_SERVER in keys or _LAN_KEY in keys)
    _require_dev_for_devversion(identity, keys)
    extra: dict[str, Any] = {}
    async with container.uow() as uow:
        if _LAN_SERVER in keys:
            old_server = _norm_server(await container.bot_config.value(uow, _LAN_SERVER))
            default = _norm_server(registry.spec(_LAN_SERVER).default)
            if old_server != default and _LAN_KEY not in keys:
                keys.append(_LAN_KEY)  # the key belongs to the server being dropped
            extra = {"lan_server_old": old_server, "lan_server_new": default}
        await container.bot_config.reset(uow, keys)
        await audit(uow, identity, "settings.reset", None, keys=keys, **extra)
        await uow.commit()
    return {"ok": True, "reset": keys}
