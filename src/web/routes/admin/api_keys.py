"""Admin: read-only API keys for AI agents (owner-only) — «Настройки → Ключи ИИ-агентов».

A key (``cwk_...``) is shown once on creation; only its SHA-256 hash is stored. What a key may
do is enforced in ``deps.require_admin`` and ``api_key_auth`` (GET/HEAD only, closed screens,
rate limit, masking, journal). This screen itself is closed to keys.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.core.enums import Role
from src.infrastructure.database.models.admin_api_key import AdminApiKey
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import OkOut, audit, iso
from src.web.routes.admin.api_key_auth import (
    generate_api_key,
    hash_api_key,
    read_key_log,
)
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/admin-keys")

# Admin API path prefixes a key can be limited to (a key with allowed_screens=None reads all of
# them). Backend prefixes, not frontend routes: the agent calls the API directly.
KEY_SCREENS: dict[str, str] = {
    "dashboard": "Обзор",
    "stats": "Статистика",
    "analytics": "Аналитика",
    "users": "Пользователи",
    "plans": "Тарифы",
    "constructor": "Конструктор тарифа",
    "promocodes": "Промокоды",
    "promogroups": "Промогруппы",
    "referral": "Рефералы",
    "campaigns": "Кампании",
    "partners": "Партнёры",
    "sales": "Акции",
    "broadcasts": "Рассылки",
    "smart-reminder": "Автоуведомления",
    "winback": "Возврат ушедших",
    "holidays": "Праздники",
    "notifications": "Уведомления",
    "reminders": "Напоминания",
    "payments": "Платежи",
    "withdrawals": "Выводы",
    "tickets": "Тикеты",
    "support-channels": "Каналы поддержки",
    "ai-support": "ИИ-поддержка",
    "blacklist": "Чёрный список",
    "servers": "Серверы",
    "routers": "Роутеры",
    "settings": "Настройки",
    "bot-menu": "Меню бота",
    "miniapp": "Мини-приложение",
    "report-topics": "Группа отчётов",
}


def _require_owner(identity: AdminIdentity) -> None:
    if identity.role is not Role.OWNER:
        raise HTTPException(403, "owner only")


def _clean_screens(screens: list[str] | None) -> list[str] | None:
    if screens is None:
        return None
    return [s for s in dict.fromkeys(screens) if s in KEY_SCREENS]


def _status(k: AdminApiKey) -> str:
    if k.revoked_at is not None:
        return "revoked"
    if k.expires_at <= dt.datetime.now(dt.UTC):
        return "expired"
    return "active"


def _row(k: AdminApiKey) -> dict[str, Any]:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "status": _status(k),
        "allowed_screens": k.allowed_screens,
        "created_at": iso(k.created_at),
        "expires_at": iso(k.expires_at),
        "last_used_at": iso(k.last_used_at),
        "revoked_at": iso(k.revoked_at),
    }


@router.get("")
async def list_keys(
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    _require_owner(identity)
    async with container.uow() as uow:
        rows = await uow.session.scalars(select(AdminApiKey).order_by(AdminApiKey.id.desc()))
        keys = rows.all()
    return {
        "items": [_row(k) for k in keys],
        "screens": [{"key": k, "label": v} for k, v in KEY_SCREENS.items()],
    }


class KeyCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=48)
    days: Literal[30, 90, 365] = 90
    # None -> every screen open to keys; a list -> only those (plus auth/me).
    allowed_screens: list[str] | None = None


@router.post("")
async def create_key(
    body: KeyCreateIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Issue a key. The plain key is in this response only — it is never shown again."""
    _require_owner(identity)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    key = generate_api_key()
    async with container.uow() as uow:
        row = AdminApiKey(
            name=name,
            key_hash=hash_api_key(key),
            prefix=key[:8],
            created_by=identity.user_id,
            created_at=dt.datetime.now(dt.UTC),
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=body.days),
            allowed_screens=_clean_screens(body.allowed_screens),
        )
        await uow.admin_api_keys.add(row)
        await audit(
            uow,
            identity,
            "admin_keys.create",
            f"admin_key:{row.id}",
            name=name,
            days=body.days,
            screens=row.allowed_screens,
        )
        await uow.commit()
        out = _row(row)
    return {"key": key, "item": out}


@router.post("/{key_id}/revoke")
async def revoke_key(
    key_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    _require_owner(identity)
    async with container.uow() as uow:
        row = await uow.admin_api_keys.get(key_id)
        if row is None:
            raise HTTPException(404, "key not found")
        if row.revoked_at is None:
            row.revoked_at = dt.datetime.now(dt.UTC)
            await audit(uow, identity, "admin_keys.revoke", f"admin_key:{key_id}", name=row.name)
            await uow.commit()
    return OkOut()


@router.get("/{key_id}/log")
async def key_log(
    key_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Last requests made with the key (newest first): time, method, path, status."""
    _require_owner(identity)
    async with container.uow() as uow:
        if await uow.admin_api_keys.get(key_id) is None:
            raise HTTPException(404, "key not found")
    try:
        items = await read_key_log(container.redis, key_id)
    except Exception:
        items = []
    return {"items": items}
