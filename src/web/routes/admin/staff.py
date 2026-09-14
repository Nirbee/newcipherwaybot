"""Admin: staff/admin accounts (owner-only) — scoped access via allowed_admin_screens.

A restricted account can log into /admin like any staff member, but require_admin confines
every request to the admin API path-prefixes listed here. This is deliberately a hand-picked,
verified subset of screens, not every mounted admin sub-router — several have no prefix of
their own or use a different word than their frontend screen (plans<->tariffs, bot-menu<->
bot-buttons, etc.); guessing those would silently make a granted screen not actually work.
Add more here only once each is individually confirmed to match its frontend path exactly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.application.services.ids import generate_referral_code
from src.core.enums import AuthType, Role, UserStatus
from src.core.security import hash_password
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import OkOut, audit, iso
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/admins")

SCOPABLE_SCREENS = (
    "routers", "users", "servers", "settings", "stats", "broadcasts", "notifications",
    "reminders", "campaigns", "partners", "sales", "ai-support", "blacklist", "miniapp",
)
_PROTECTED_ROLES = (Role.OWNER, Role.DEV)  # never editable/revocable through this screen


def _require_owner(identity: AdminIdentity) -> None:
    if identity.role is not Role.OWNER:
        raise HTTPException(403, "owner only")


def _clean_screens(screens: list[str] | None) -> list[str] | None:
    if screens is None:
        return None
    return [s for s in screens if s in SCOPABLE_SCREENS]


def _row(u: User) -> dict[str, Any]:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role.name,
        "status": u.status.value,
        "allowed_screens": u.allowed_admin_screens,
        "created_at": iso(u.created_at),
    }


@router.get("")
async def list_admins(
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    _require_owner(identity)
    async with container.uow() as uow:
        rows = await uow.session.scalars(
            select(User).where(User.role.in_((Role.ADMIN, *_PROTECTED_ROLES))).order_by(User.id)
        )
        users = rows.all()
    return {"items": [_row(u) for u in users], "scopable_screens": list(SCOPABLE_SCREENS)}


class AdminCreateIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    # None -> full ADMIN access (every screen); [] or a subset -> confined to just those.
    allowed_screens: list[str] | None = None


@router.post("")
async def create_admin(
    body: AdminCreateIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    _require_owner(identity)
    username = body.username.strip().lstrip("@")
    async with container.uow() as uow:
        if await uow.users.find_one(username=username):
            raise HTTPException(409, "username already taken")
        user = User(
            username=username,
            auth_type=AuthType.EMAIL,
            role=Role.ADMIN,
            referral_code=generate_referral_code(),
            password_hash=hash_password(body.password),
            allowed_admin_screens=_clean_screens(body.allowed_screens),
        )
        await uow.users.add(user)
        await audit(
            uow, identity, "admins.create", f"user:{user.id}",
            screens=user.allowed_admin_screens,
        )
        await uow.commit()
        row = _row(user)
    return row


class AdminPatchIn(BaseModel):
    allowed_screens: list[str] | None = None
    password: str | None = Field(None, min_length=8, max_length=128)
    status: str | None = None  # "active" | "blocked"


@router.patch("/{admin_id}")
async def patch_admin(
    admin_id: int,
    body: AdminPatchIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    _require_owner(identity)
    async with container.uow() as uow:
        user = await uow.users.get(admin_id)
        if user is None or not user.role.is_staff:
            raise HTTPException(404, "admin not found")
        if user.role in _PROTECTED_ROLES:
            raise HTTPException(400, "this account can't be edited here")
        # allowed_screens sent explicitly (incl. null, meaning "full access") vs simply
        # omitted from the request body must be told apart — only the former should change it.
        if "allowed_screens" in body.model_fields_set:
            user.allowed_admin_screens = _clean_screens(body.allowed_screens)
        if body.password:
            user.password_hash = hash_password(body.password)
        if body.status == "blocked":
            user.status = UserStatus.BLOCKED
        elif body.status == "active":
            user.status = UserStatus.ACTIVE
        await audit(uow, identity, "admins.patch", f"user:{admin_id}")
        await uow.commit()
    return OkOut()


@router.post("/{admin_id}/revoke")
async def revoke_admin(
    admin_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """Demotes to a regular USER and clears the password hash — the account and its history
    stay (never deleted), but it can no longer log into /admin. Re-granting later means setting
    a new password via PATCH after promoting the role back (not exposed here on purpose — this
    endpoint is a one-way "kick", not a toggle)."""
    _require_owner(identity)
    async with container.uow() as uow:
        user = await uow.users.get(admin_id)
        if user is None or not user.role.is_staff:
            raise HTTPException(404, "admin not found")
        if user.role in _PROTECTED_ROLES:
            raise HTTPException(400, "this account can't be revoked here")
        user.role = Role.USER
        user.password_hash = None
        user.allowed_admin_screens = None
        await audit(uow, identity, "admins.revoke", f"user:{admin_id}")
        await uow.commit()
    return OkOut()
