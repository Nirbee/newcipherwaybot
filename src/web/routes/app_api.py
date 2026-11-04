"""API for the CipherWay Windows app (``/api/app/v1``) — support chat straight from the app.

The app knows nothing but the customer's Remnawave subscription link
``https://<sub-page>/<token>`` (token = the panel shortUuid), so that token IS the
credential: ``X-Subscription-Token: <token>``. The PANEL is the source of truth for whether a
token is live: format check (no I/O for garbage) → Redis cache (bound to the link stored on
our subscription, so a reset/revoke that changes it drops the cache at once) → the panel
(resolve by shortUuid only, exact match) → our subscription via the panel-webhook keys.

Every failure answers the same 401 — never «no such subscription» vs «wrong token»; known-bad
tokens are remembered for 10 min. Per-IP (IPv6 by /64) and per-token fixed-window limits
(429), a per-IP cap on FAILED resolves, a global cap on panel resolves (503) and per-user
daily quotas. The token proves ONE subscription, not the account: the app sees only its own
thread of THAT subscription (Ticket.channel=APP + subscription_id; the «💻 » subject prefix
is just a hint for the admin queue), and the AI is pinned to that subscription and never
discloses the link. An expired/disabled subscription may still write to support. No CORS:
the app calls from Electron's main process.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from src.application.services.connection import link_token
from src.core.enums import UserStatus
from src.core.exceptions import RemnawaveAuthError, RemnawaveTransientError
from src.core.logging import get_logger
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.web import support
from src.web.deps import get_container
from src.web.routes.cabinet_auth import _client_ip, _rate_limit
from src.web.routes.panel import resolve_subscription
from src.web.support import SupportIn

log = get_logger(__name__)

router = APIRouter(prefix="/api/app/v1", tags=["app"])

# Panel shortUuid: URL-safe, bounded — checked before any I/O.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")
_CACHE_TTL = 600  # a resolved token → subscription, so polling doesn't hit the panel
_BAD_TTL = 600  # a token the panel/DB didn't recognise — answered 401 without a lookup
_FAIL_LIMIT = 20  # failed resolves per IP per window, then 429 without touching the panel
_FAIL_WINDOW = 600
_PANEL_RESOLVE_LIMIT = 300  # panel lookups per minute across ALL clients, then 503
# Requests per minute, per IP and per token: chat polling / text messages / screenshots.
_LIMITS = {"get": 30, "post": 10, "photo": 5}
# Per-user daily quotas on writes (the owner reads every one of them).
_DAILY = {"post": 200, "photo": 30}


@dataclass(frozen=True)
class AppIdentity:
    """Who the token proved: the customer AND the one subscription the link belongs to."""

    user: User
    subscription_id: int


def _unauthorized() -> HTTPException:
    return HTTPException(401, "unauthorized")


def _token_key(token: str) -> str:
    """Tokens are never stored or logged in the clear — only this digest."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def _url_digest(url: str | None) -> str:
    return hashlib.sha256((url or "").encode()).hexdigest()[:16]


def _cache_key(token: str) -> str:
    return f"app:sub:{_token_key(token)}"


def _ip_bucket(ip: str) -> str:
    """Rate-limit identity: an IPv6 client owns a whole /64, so count it as one."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)  # ::ffff:1.2.3.4 is an IPv4 client, not a /64
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return ip


async def forget_link(container: AppContainer, subscription_url: str | None) -> None:
    """Drop the cached app session for a link that was just rotated (reset-link), so the
    old token answers 401 immediately instead of after the cache TTL."""
    token = link_token(subscription_url)
    if not token:
        return
    with contextlib.suppress(Exception):
        await container.redis.delete(_cache_key(token))


async def _load(container: AppContainer, sub_id: int) -> tuple[Any, User | None]:
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(sub_id)
        user = await uow.users.get(sub.user_id) if sub is not None else None
    return sub, user


async def _cached(container: AppContainer, key: str) -> AppIdentity | None:
    """A cached resolve, valid only while our subscription still carries the same link."""
    raw: Any = None
    with contextlib.suppress(Exception):
        raw = await container.redis.get(key)
    if not raw:
        return None
    sub_id_s, _, digest = str(raw).partition(":")
    sub, user = (None, None)
    if sub_id_s.isdigit():
        sub, user = await _load(container, int(sub_id_s))
    if sub is not None and user is not None and digest == _url_digest(sub.subscription_url):
        return AppIdentity(user=user, subscription_id=sub.id)
    with contextlib.suppress(Exception):
        await container.redis.delete(key)  # link rotated / sub gone / merged — resolve afresh
    return None


async def _too_many_failures(container: AppContainer, ip: str) -> bool:
    if not ip:
        return False
    try:
        n = int(await container.redis.get(f"rl:auth:app-badtoken:{ip}") or 0)
    except Exception:  # fail-open, like _rate_limit
        return False
    return n >= _FAIL_LIMIT


async def _panel_budget(container: AppContainer) -> None:
    """Global ceiling on panel lookups — a distributed guess storm must not hammer the panel."""
    key = "rl:app:panel-resolve"
    try:
        n = int(await container.redis.incr(key))
        if n == 1:
            await container.redis.expire(key, 60)
    except Exception:
        return
    if n > _PANEL_RESOLVE_LIMIT:
        raise HTTPException(503, "temporarily unavailable")


async def _resolve_sub_id(container: AppContainer, token: str) -> int | None:
    """Our subscription id for a LIVE link token (confirmed by the panel), or None."""
    await _panel_budget(container)
    try:
        panel_user = await container.remnawave_client.get_user_by_short_uuid(token)
    except (RemnawaveAuthError, RemnawaveTransientError) as exc:
        # Panel down ≠ bad token: don't make the app forget a valid link over an outage.
        raise HTTPException(503, "temporarily unavailable") from exc
    if panel_user is None or not hmac.compare_digest(panel_user.short_id.encode(), token.encode()):
        return None
    async with container.uow() as uow:
        sub = await resolve_subscription(uow, panel_user)
    return sub.id if sub is not None else None


async def _identity_for_token(container: AppContainer, token: str, ip: str) -> AppIdentity:
    key = _cache_key(token)
    cached = await _cached(container, key)
    if cached is not None:
        return cached
    bad_key = f"app:bad:{_token_key(token)}"
    known_bad: Any = None
    with contextlib.suppress(Exception):
        known_bad = await container.redis.get(bad_key)
    if known_bad:
        raise _unauthorized()
    if await _too_many_failures(container, ip):
        raise HTTPException(429, "too many attempts — try again later")
    sub_id = await _resolve_sub_id(container, token)
    sub, user = await _load(container, sub_id) if sub_id is not None else (None, None)
    if sub is None or user is None:
        with contextlib.suppress(Exception):
            await container.redis.set(bad_key, "1", ex=_BAD_TTL)
        await _rate_limit(container, "app-badtoken", ip, limit=_FAIL_LIMIT, window=_FAIL_WINDOW)
        raise _unauthorized()
    with contextlib.suppress(Exception):
        await container.redis.set(
            key, f"{sub.id}:{_url_digest(sub.subscription_url)}", ex=_CACHE_TTL
        )
    return AppIdentity(user=user, subscription_id=sub.id)


def app_identity(kind: str) -> Callable[..., Awaitable[AppIdentity]]:
    """Dependency: the customer behind ``X-Subscription-Token``, rate-limited per ``kind``."""
    limit = _LIMITS[kind]
    daily = _DAILY.get(kind)

    async def dependency(
        request: Request, container: AppContainer = Depends(get_container)
    ) -> AppIdentity:
        token = request.headers.get("x-subscription-token", "").strip()
        if not _TOKEN_RE.fullmatch(token):
            raise _unauthorized()
        ip = _ip_bucket(_client_ip(request))
        await _rate_limit(container, f"app-{kind}", ip, limit=limit, window=60)
        await _rate_limit(container, f"app-{kind}-t", _token_key(token), limit=limit, window=60)
        ident = await _identity_for_token(container, token, ip)
        if ident.user.status is UserStatus.BLOCKED:
            raise HTTPException(403, "blocked")
        if daily is not None:
            await _rate_limit(
                container, f"app-day-{kind}", str(ident.user.id), limit=daily, window=86400
            )
        return ident

    return dependency


@router.get("/me")
async def me(ident: AppIdentity = Depends(app_identity("get"))) -> dict[str, Any]:
    """Just enough for the app's support screen — no nickname / e-mail / phone / balance."""
    return {"ok": True, "has_telegram": ident.user.telegram_id is not None}


@router.get("/support")
async def support_history(
    ident: AppIdentity = Depends(app_identity("get")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """The app's own support conversation for this subscription — same shape as
    ``GET /api/cabinet/support``; bot/cabinet threads stay private to the account."""
    return await support.support_history(
        container, ident.user, app_subscription_id=ident.subscription_id
    )


@router.post("/support")
async def support_send(
    body: SupportIn,
    ident: AppIdentity = Depends(app_identity("post")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Post a support message; the AI answers/escalates, pinned to the token's subscription."""
    return await support.support_send(
        container, ident.user, body.text, app_subscription_id=ident.subscription_id
    )


@router.post("/support/photo")
async def support_send_photo(
    request: Request,
    ident: AppIdentity = Depends(app_identity("photo")),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """A screenshot into the support conversation (multipart ``file`` + optional ``text``).

    The body is parsed HERE, after the token and the limits passed — not by FastAPI before
    the dependency runs — and only with a declared, bounded Content-Length."""
    async with support.photo_form(request) as (file, text):
        return await support.support_send_photo(
            container, ident.user, file, text, app_subscription_id=ident.subscription_id
        )
