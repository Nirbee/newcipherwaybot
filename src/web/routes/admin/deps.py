"""Admin API dependencies: JWT bearer auth resolving to an admin ``User``, or a read-only
API key (``cwk_...``) for AI agents — see ``api_key_auth``."""

from __future__ import annotations

import datetime as dt
import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from src.core.enums import Role, UserStatus
from src.core.security import jwt_decode
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin.api_key_auth import (
    API_KEY_PREFIX,
    KEY_STATE_ATTR,
    hash_api_key,
    key_path_blocked,
    key_rate_limited,
    screen_of,
)

# last_used_at of an API key is written at most this often (not on every request).
_KEY_LAST_USED_EVERY = dt.timedelta(minutes=1)


@dataclass(slots=True)
class AdminIdentity:
    user_id: int
    username: str
    role: Role
    allowed_screens: list[str] | None = None
    api_key_id: int | None = None  # set when the request came with a read-only API key


def _unauthorized(detail: str = "unauthorized") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def _require_api_key(request: Request, container: AppContainer, key: str) -> AdminIdentity:
    """Read-only API key: GET/HEAD on /api/admin/* only, minus the screens closed to keys.

    The key dies with its creator's owner rights (blocked / demoted owner -> 401)."""
    path = request.url.path
    if not path.startswith("/api/admin/"):
        raise _unauthorized()
    digest = hash_api_key(key)
    now = dt.datetime.now(dt.UTC)
    async with container.uow() as uow:
        row = await uow.admin_api_keys.find_one(key_hash=digest)
        if row is None or not hmac.compare_digest(row.key_hash, digest):
            raise _unauthorized()
        if row.revoked_at is not None or row.expires_at <= now:
            raise _unauthorized("api key revoked or expired")
        creator = await uow.users.get(row.created_by)
        if (
            creator is None
            or creator.status is not UserStatus.ACTIVE
            or creator.role is not Role.OWNER
        ):
            raise _unauthorized("api key revoked or expired")
        if row.last_used_at is None or now - row.last_used_at >= _KEY_LAST_USED_EVERY:
            row.last_used_at = now
            await uow.commit()
        key_id, name, screens, created_by = row.id, row.name, row.allowed_screens, row.created_by

    # From here on the request belongs to the key: AdminKeyResponseFilter masks the answer
    # and writes the request (whatever its outcome) to the key's journal.
    setattr(request.state, KEY_STATE_ATTR, key_id)
    if await key_rate_limited(container.redis, key_id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="слишком много запросов")
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="ключ только для чтения")
    if key_path_blocked(path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="этот раздел закрыт для ключей")
    if screens is not None and screen_of(path) not in ("auth", *screens):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="этот раздел не входит в доступ ключа"
        )
    return AdminIdentity(
        user_id=created_by,
        username=f"ключ: {name}",
        role=Role.PREVIEW,  # read-only; owner-only endpoints refuse it as well
        allowed_screens=screens,
        api_key_id=key_id,
    )


async def require_admin(
    request: Request, container: AppContainer = Depends(get_container)
) -> AdminIdentity:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _unauthorized()
    token = auth.removeprefix("Bearer ")
    if token.startswith(API_KEY_PREFIX):
        return await _require_api_key(request, container, token)
    payload = jwt_decode(token, container.settings.app.jwt_secret)
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
        screen = screen_of(request.url.path)
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
