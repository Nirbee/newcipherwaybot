"""Admin API dependencies: JWT bearer auth resolving to an admin ``User``."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from src.core.enums import Role, UserStatus
from src.core.security import jwt_decode
from src.infrastructure.di import AppContainer
from src.web.deps import get_container


@dataclass(slots=True)
class AdminIdentity:
    user_id: int
    username: str
    role: Role
    allowed_screens: list[str] | None = None


def _unauthorized(detail: str = "unauthorized") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _screen_of(path: str) -> str:
    """First path segment after /api/admin/, e.g. '/api/admin/routers/5/rotate' -> 'routers'."""
    rest = path.removeprefix("/api/admin/").removeprefix("/api/admin")
    return rest.split("/", 1)[0]


async def require_admin(
    request: Request, container: AppContainer = Depends(get_container)
) -> AdminIdentity:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _unauthorized()
    payload = jwt_decode(auth.removeprefix("Bearer "), container.settings.app.jwt_secret)
    if payload is None or payload.get("scope") != "admin":
        raise _unauthorized()

    async with container.uow() as uow:
        user = await uow.users.get(int(payload["sub"]))
    if user is None or user.status is not UserStatus.ACTIVE:
        raise _unauthorized("admin access revoked")

    # Read-only demo sessions: PREVIEW role may look at everything but change nothing.
    if user.role is Role.PREVIEW:
        if not container.settings.admin.demo_enabled:
            raise _unauthorized("demo mode disabled")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="demo is read-only",
            )
    elif not user.role.is_staff:
        raise _unauthorized("admin access revoked")

    # A restricted staff account (allowed_admin_screens set, even to an empty list) can only
    # reach the admin API prefixes explicitly granted to it — "auth" (login/me/logout) always
    # passes so the frontend can still identify the session and redirect appropriately.
    if user.allowed_admin_screens is not None:
        screen = _screen_of(request.url.path)
        if screen not in ("auth", *user.allowed_admin_screens):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="this admin account has no access to this screen",
            )
    return AdminIdentity(
        user_id=user.id,
        username=user.username or f"id{user.id}",
        role=user.role,
        allowed_screens=user.allowed_admin_screens,
    )
