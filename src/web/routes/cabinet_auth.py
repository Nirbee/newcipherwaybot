"""Web-cabinet email auth: register / verify / login / refresh / logout.

Lets a person register and buy a subscription from a website WITHOUT Telegram.
Sessions are short-lived access JWTs (15 min) + rotating refresh tokens (7 days,
SHA-256-hashed in the DB). The access JWT is signed with APP__JWT_SECRET (never the
bot token — that would let a leaked bot token forge cabinet sessions). Once logged
in, the same ``/api/cabinet/*`` endpoints (purchase, me, devices…) work over the
``Authorization: Bearer <access>`` header.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import ipaddress
import re as _re
import secrets
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

    from src.infrastructure.database.uow import UnitOfWork

from src.application.services.account_link import (
    GUEST_FLAG,
    PW_CHANGED_AT,
    is_guest_account,
    password_changed_at,
)
from src.application.services.ids import generate_referral_code
from src.core.config import get_settings
from src.core.enums import AuthType, Currency, UserStatus
from src.core.logging import get_logger
from src.core.security import hash_password, jwt_decode, jwt_encode, verify_password
from src.infrastructure.database.models.cabinet_token import CabinetRefreshToken
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.web.deps import get_container

router = APIRouter(prefix="/api/cabinet/auth", tags=["cabinet-auth"])
log = get_logger(__name__)

_ACCESS_TTL = 15 * 60
_REFRESH_TTL_DAYS = 7
_VERIFY_TTL_HOURS = 24

# A REAL scrypt hash of a random secret, used as the "miss" comparand in login so an
# unknown e-mail runs the full 16 MiB scrypt just like a hit. The old placeholder
# "scrypt$1$1$1$x$x" made verify_password raise instantly (n=1 isn't a power of two),
# returning in ~0 ms and turning login into an e-mail-existence timing oracle.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(24))


def _jwt_secret(container: AppContainer) -> str:
    return container.settings.app.jwt_secret


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _access_token(container: AppContainer, user: User) -> str:
    return jwt_encode(
        {"sub": user.id, "type": "access", "web": True},
        _jwt_secret(container),
        _ACCESS_TTL,
    )


async def _issue_refresh(container: AppContainer, user_id: int, *, device: str | None) -> str:
    token = secrets.token_urlsafe(32)
    async with container.uow() as uow:
        uow.session.add(
            CabinetRefreshToken(
                user_id=user_id,
                token_hash=_hash(token),
                device_info=(device or "")[:256] or None,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=_REFRESH_TTL_DAYS),
            )
        )
        await uow.commit()
    return token


def _ensure_active(user: User) -> None:
    """A BLOCKED user must not get (or keep) a web session — the bot + tma paths already
    enforce this; the email/OAuth/JWT path has to match, or a blocked user keeps full access."""
    if user.status is UserStatus.BLOCKED:
        raise HTTPException(403, "account blocked")


async def _auth_response(
    container: AppContainer, user: User, *, device: str | None
) -> dict[str, Any]:
    _ensure_active(user)  # single choke point: never mint a session for a blocked account
    refresh = await _issue_refresh(container, user.id, device=device)
    return {
        "access_token": _access_token(container, user),
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": _ACCESS_TTL,
        "user": {"id": user.id, "email": user.email, "email_verified": user.email_verified},
    }


async def _require_web_enabled(container: AppContainer) -> None:
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "WEB_CABINET_ENABLED")):
            raise HTTPException(403, "web cabinet is disabled")


@functools.lru_cache(maxsize=4)
def _proxy_networks(
    cidrs: tuple[str, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(n, strict=False) for n in cidrs)


def _is_proxy_peer(host: str) -> bool:
    """Only our own reverse proxy (WEB__TRUSTED_PROXIES) may vouch for the client address.
    A request that arrives straight from anywhere else set those headers itself."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    nets = _proxy_networks(tuple(get_settings().web.trusted_proxies))
    return any(addr in net for net in nets)


def _client_ip(request: Request) -> str:
    # Proxy headers are trusted ONLY from a trusted proxy peer — otherwise any client could
    # send its own per request and defeat every limiter. From the proxy: the RIGHTMOST
    # X-Forwarded-For hop (Caddy sets XFF to the peer it saw; nginx $proxy_add_x_forwarded_for
    # appends it on the right — anything to the left is client-controlled), X-Real-IP only as
    # a fallback, else the socket peer.
    peer = request.client.host if request.client else ""
    if not _is_proxy_peer(peer):
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    hop = xff.split(",")[-1].strip() if xff else ""
    if hop:
        return hop
    real = request.headers.get("x-real-ip", "").strip()
    return real or peer


async def _over_limit(
    container: AppContainer, bucket: str, ident: str, *, limit: int, window: int
) -> bool:
    """Count one hit in a fixed window; True once ``limit`` is exceeded. Fail-OPEN."""
    if not ident:
        return False
    key = f"rl:auth:{bucket}:{ident}"
    try:
        n = int(await container.redis.incr(key))
        if n == 1:
            await container.redis.expire(key, window)
    except Exception:  # availability over enforcement — never lock out on a Redis hiccup
        return False
    return n > limit


async def _rate_limit(
    container: AppContainer, bucket: str, ident: str, *, limit: int, window: int
) -> None:
    """Fixed-window per-identity limiter for the auth endpoints (brute-force / enumeration /
    account-creation spam). Fail-OPEN: a Redis hiccup must never lock real users out."""
    if await _over_limit(container, bucket, ident, limit=limit, window=window):
        raise HTTPException(429, "too many attempts — try again later")


class RegisterIn(BaseModel):
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email")
        return v


@router.post("/register")
async def register(
    body: RegisterIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    await _require_web_enabled(container)
    await _rate_limit(container, "register", _client_ip(request), limit=10, window=3600)
    email = str(body.email).strip().lower()
    async with container.uow() as uow:
        if await uow.users.get_by_email(email) is not None:
            raise HTTPException(409, "email already registered")
        verify = bool(await container.bot_config.value(uow, "CABINET_EMAIL_VERIFICATION"))
        cabinet_url = str(await container.bot_config.value(uow, "CABINET_URL") or "")
        user = User(
            auth_type=AuthType.EMAIL,
            email=email,
            password_hash=hash_password(body.password),
            email_verified=not verify,
            referral_code=generate_referral_code(),
            currency=Currency.RUB,
        )
        await uow.users.add(user)
        token = ""
        if verify:
            token = secrets.token_hex(32)
            user.notification_settings = {
                **(user.notification_settings or {}),
                "verify_token": token,
                "verify_expires": (
                    dt.datetime.now(dt.UTC) + dt.timedelta(hours=_VERIFY_TTL_HOURS)
                ).isoformat(),
            }
        try:
            await uow.commit()
        except IntegrityError as exc:  # concurrent registration lost the uq_users_email race
            raise HTTPException(409, "email already registered") from exc
        user_id = user.id

    if not verify:
        async with container.uow() as uow:
            fresh = await uow.users.get(user_id)
        if fresh is None:
            raise HTTPException(500, "registration failed")
        return await _auth_response(container, fresh, device=None)

    link = f"{cabinet_url.rstrip('/')}/verify-email?token={token}" if cabinet_url else token
    mailer = await container.build_mailer()
    await mailer.send(
        email,
        "Подтверждение регистрации",
        f"Подтвердите e-mail, перейдя по ссылке:\n{link}\n\nСсылка действует 24 часа.",  # noqa: RUF001
    )
    return {"ok": True, "requires_verification": True, "email": email}


class VerifyIn(BaseModel):
    token: str = Field(..., min_length=8, max_length=128)


@router.post("/verify")
async def verify_email(
    body: VerifyIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    await _rate_limit(container, "verify", _client_ip(request), limit=30, window=3600)
    async with container.uow() as uow:
        user = await uow.users.find_by_verify_token(body.token)
        if user is None:
            raise HTTPException(400, "invalid or expired token")
        settings_ = user.notification_settings or {}
        expires = settings_.get("verify_expires")
        if expires and dt.datetime.fromisoformat(expires) < dt.datetime.now(dt.UTC):
            raise HTTPException(400, "verification link expired")
        user.email_verified = True
        user.notification_settings = {
            k: v for k, v in settings_.items() if k not in ("verify_token", "verify_expires")
        }
        await uow.commit()
        user_id = user.id
        fresh = await uow.users.get(user_id)
    if fresh is None:
        raise HTTPException(400, "user gone")
    return await _auth_response(container, fresh, device=request.headers.get("user-agent"))


class LoginIn(BaseModel):
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()


@router.post("/login")
async def login(
    body: LoginIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    await _require_web_enabled(container)
    await _rate_limit(container, "login", _client_ip(request), limit=10, window=300)
    await _rate_limit(container, "login_email", str(body.email), limit=10, window=300)
    async with container.uow() as uow:
        user = await uow.users.get_by_email(str(body.email).strip().lower())
        stored = user.password_hash if (user and user.password_hash) else _DUMMY_PASSWORD_HASH
        # verify even on miss (constant-ish time) to avoid user enumeration
        if not verify_password(body.password, stored):
            raise HTTPException(401, "invalid credentials")
        if user is None or not user.email_verified:
            raise HTTPException(403, "email not verified")
        verified_user = user
    return await _auth_response(container, verified_user, device=request.headers.get("user-agent"))


class RefreshIn(BaseModel):
    refresh_token: str = Field(..., min_length=8)


@router.post("/refresh")
async def refresh(
    body: RefreshIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    from sqlalchemy import update

    await _require_web_enabled(container)  # kill-switch gates rotation, not just fresh logins
    token_hash = _hash(body.refresh_token)
    now = dt.datetime.now(dt.UTC)
    async with container.uow() as uow:
        row = await uow.cabinet_tokens.find_one(token_hash=token_hash)
        if row is None or row.revoked_at is not None or row.expires_at < now:
            raise HTTPException(401, "invalid refresh token")
        # Atomic spend: WHERE revoked_at IS NULL so two concurrent requests with the same
        # token can't both pass the read-check and both mint a session (double-spend race).
        result = await uow.session.execute(
            update(CabinetRefreshToken)
            .where(
                CabinetRefreshToken.id == row.id,
                CabinetRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        if (cast("CursorResult[Any]", result).rowcount or 0) == 0:
            raise HTTPException(401, "invalid refresh token")  # lost the rotation race
        user = await uow.users.get(row.user_id)
        if user is None:
            raise HTTPException(401, "user gone")
        await uow.commit()
    return await _auth_response(container, user, device=request.headers.get("user-agent"))


@router.post("/logout")
async def logout(
    body: RefreshIn, container: AppContainer = Depends(get_container)
) -> dict[str, bool]:
    async with container.uow() as uow:
        row = await uow.cabinet_tokens.find_one(token_hash=_hash(body.refresh_token))
        if row is not None and row.revoked_at is None:
            row.revoked_at = dt.datetime.now(dt.UTC)
            await uow.commit()
    return {"ok": True}


async def web_user_from_bearer(request: Request, container: AppContainer) -> User | None:
    """Resolve a web-cabinet user from an ``Authorization: Bearer <access>`` header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = jwt_decode(auth.removeprefix("Bearer "), _jwt_secret(container))
    if payload is None or payload.get("type") != "access" or not payload.get("web"):
        return None
    async with container.uow() as uow:
        # The kill-switch must gate EXISTING sessions too, not just new logins — otherwise
        # turning the web cabinet off (e.g. mid-abuse) leaves every already-logged-in user
        # with full /api/cabinet/* access and endless refresh-token rotation.
        if not bool(await container.bot_config.value(uow, "WEB_CABINET_ENABLED")):
            return None
        user = await uow.users.get(int(payload["sub"]))
    if user is None or user.status is UserStatus.BLOCKED:
        return None  # blocked -> cabinet_user() raises 401, same as the bot/tma block
    # A password reset ends sessions opened before it. Strict «<»: the session the reset
    # itself hands out (same second) stays valid.
    if int(payload.get("iat") or 0) < password_changed_at(user):
        return None
    return user


# --- guest purchase (buy without any prior registration) --------------------

# GUEST_FLAG marks an account the guest purchase created with a random password nobody knows.
# Only such an account gets a one-click login link in its letters (see user_mail), and until
# the mailbox owner claims it, it can't be bound to anything else (Telegram, OAuth).
GUEST_CLAIM_FIRST = "Сначала задайте пароль по ссылке из письма (или «Забыли пароль?»)"


def clear_guest_flag(user: User) -> None:
    settings_ = user.notification_settings or {}
    if GUEST_FLAG in settings_:
        user.notification_settings = {k: v for k, v in settings_.items() if k != GUEST_FLAG}


async def _claim_mailbox(uow: UnitOfWork, user: User, now: dt.datetime) -> None:
    """The owner of the address just proved the mailbox (reset link, OAuth with that e-mail).

    Whoever pre-claimed the account before them (a guest purchase on someone else's address
    hands its buyer an auto-login token) is shut out: the guest flag goes, every session /
    auto-login token / link code minted before ``now`` dies (PW_CHANGED_AT), and every refresh
    token is revoked. Commit is the caller's."""
    from sqlalchemy import update

    user.notification_settings = {
        **{k: v for k, v in (user.notification_settings or {}).items() if k != GUEST_FLAG},
        PW_CHANGED_AT: int(now.timestamp()),
    }
    await uow.session.execute(
        update(CabinetRefreshToken)
        .where(
            CabinetRefreshToken.user_id == user.id,
            CabinetRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


def _mask_email(email: str | None) -> str:
    """a***@example.com — enough to recognise your own address, not to harvest someone's."""
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if local and domain else "***"


def _auto_login_token(container: AppContainer, user_id: int) -> str:
    return jwt_encode(
        {"sub": user_id, "type": "auto_login", "web": True},
        _jwt_secret(container),
        72 * 3600,
    )


class GuestPurchaseIn(BaseModel):
    email: str = Field(..., max_length=255)
    plan_id: int
    days: int = Field(..., ge=1, le=3650)  # same cap as PurchaseIn (#12)
    method: str = Field(..., min_length=2, max_length=32)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email")
        return v


@router.post("/guest/purchase")
async def guest_purchase(
    body: GuestPurchaseIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Buy a subscription with only an e-mail — no registration step.

    Auto-provisions a verified e-mail account (safer than a phantom user), starts a
    hosted gateway payment, and returns the redirect + an auto-login token so the
    success page can drop the buyer straight into the cabinet. The subscription link
    is also e-mailed on fulfilment (see ``_notify_paid``).
    """
    await _require_web_enabled(container)
    await _rate_limit(container, "guest", _client_ip(request), limit=10, window=3600)
    from src.application.dto.pricing import PurchaseRequest
    from src.core.enums import PurchaseType
    from src.web.routes.cabinet import _pay_with_gateway

    if body.method in ("balance", "stars"):
        raise HTTPException(400, "guest purchases require an online payment method")

    async with container.uow() as uow:
        # A guest supplies an e-mail without proving ownership. Marking it verified=True
        # regardless (the old behaviour) let anyone pre-seed a "claimed" account for a
        # victim's address that OAuth/password login would then merge the real owner into.
        # Honour the owner's verification policy exactly like register() does.
        verify = bool(await container.bot_config.value(uow, "CABINET_EMAIL_VERIFICATION"))
        user = await uow.users.get_by_email(body.email)
        created = False
        if user is None:
            user = User(
                auth_type=AuthType.EMAIL,
                email=body.email,
                password_hash=hash_password(secrets.token_urlsafe(12)),
                email_verified=not verify,
                referral_code=generate_referral_code(),
                currency=Currency.RUB,
                notification_settings={GUEST_FLAG: True},
            )
            await uow.users.add(user)
            created = True
        await uow.commit()
        user_id = user.id
        ptype, sub_id = await container.purchase.resolve_purchase_type(uow, user_id, body.plan_id)

    req = PurchaseRequest(
        user_id=user_id,
        plan_id=body.plan_id,
        duration_days=body.days,
        currency=Currency.RUB,
        purchase_type=ptype if not created else PurchaseType.NEW,
        subscription_id=sub_id if not created else None,
    )
    async with container.uow() as uow:
        fresh = await uow.users.get(user_id)
    if fresh is None:
        raise HTTPException(500, "guest user vanished")
    # Back to the web cabinet (where the auto-login token is picked up), not the Telegram
    # mini-app — a guest buying on the site may have no Telegram at all.
    base = (container.settings.web.public_url or "").strip().rstrip("/")
    result = await _pay_with_gateway(
        container, fresh, req, body.method, return_url=f"{base}/web/" if base else None
    )
    # Auto-login ONLY for an account THIS call just created. Reusing a pre-existing email must
    # never hand out a session for it — that would be account takeover by anyone who knows the
    # address (the guard mirrors register()'s 409 and oauth_callback()'s unverified-bind refusal).
    return {
        "redirect_url": result.get("redirect_url"),
        "auto_login_token": _auto_login_token(container, user_id) if created else None,
        "email": body.email,
    }


class AutoLoginIn(BaseModel):
    token: str = Field(..., min_length=8)


async def _auto_login_user(container: AppContainer, token: str) -> User:
    """The account an auto-login token may open — or an HTTPException. No side effects."""
    payload = jwt_decode(token, _jwt_secret(container))
    if payload is None or payload.get("type") != "auto_login":
        raise HTTPException(401, "invalid token")
    async with container.uow() as uow:
        user = await uow.users.get(int(payload["sub"]))
    if user is None:
        raise HTTPException(401, "user gone")
    # never let an auto-login token (weak proof) unlock a staff account
    if user.role.is_staff:
        raise HTTPException(403, "not allowed for staff accounts")
    # One-click login exists only for a still-unclaimed guest account: once the mailbox owner
    # set a password (or claimed it via OAuth), and for tokens older than the last reset, no.
    if not is_guest_account(user) or int(payload.get("iat") or 0) < password_changed_at(user):
        raise HTTPException(401, "invalid token")
    return user


@router.post("/login/auto")
async def login_auto(
    body: AutoLoginIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Exchange a guest auto-login token for a full session (post-purchase convenience)."""
    user = await _auto_login_user(container, body.token)
    return await _auth_response(container, user, device=request.headers.get("user-agent"))


@router.post("/login/auto/preview")
async def login_auto_preview(
    body: AutoLoginIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Which account a letter's one-click link opens (masked e-mail), so the page can ask
    «Войти как a***@…?» before exchanging it. POST, not GET: the token must stay out of the
    access logs, which is why it travels in the URL fragment in the first place."""
    await _rate_limit(container, "auto_preview", _client_ip(request), limit=30, window=3600)
    user = await _auto_login_user(container, body.token)
    return {"email": _mask_email(user.email)}


# --- «Забыли пароль?» (one-time link by e-mail) ------------------------------

_RESET_TTL = 3600
# Only the SHA-256 of a reset token is ever stored, never the token itself.
_RESET_TOKEN_PREFIX = "pwreset:t:"  # + sha256(token) -> {"uid", "ph"}
_RESET_USER_PREFIX = "pwreset:u:"  # + user id -> hash of the one live link of that account
_RESET_INVALID = "ссылка недействительна или устарела — запросите новую"


def _password_fingerprint(user: User) -> str:
    """Binds a link to the password it was issued against: once the password changes by any
    route, every older link is dead even inside its hour."""
    return _hash(user.password_hash or "")[:16]


class PasswordForgotIn(BaseModel):
    email: str = Field(..., max_length=255)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()


async def _send_reset_link(container: AppContainer, email: str) -> None:
    """Runs AFTER the response (BackgroundTasks): the lookup and the SMTP round-trip must not
    shape the answer or its timing — otherwise «forgot» becomes an «is this address
    registered?» oracle. Silent for unknown, staff and blocked accounts."""
    import json

    from src.infrastructure.services.user_mail import brand_title, cabinet_base_url, render_html

    try:
        async with container.uow() as uow:
            user = await uow.users.get_by_email(email)
            # Staff never: e-mail access must not be enough to take over an admin account
            # (mirrors the guard in login_auto).
            if (
                user is None
                or not user.email
                or user.role.is_staff
                or user.status is UserStatus.BLOCKED
            ):
                return
            cabinet = await cabinet_base_url(container, uow)
            brand = await brand_title(uow)
            user_id, to, fingerprint = user.id, user.email, _password_fingerprint(user)
        if not cabinet:
            log.warning("password reset: no cabinet URL configured", user_id=user_id)
            return
        token = secrets.token_urlsafe(32)
        token_hash = _hash(token)
        # One live link per account: a fresh request kills the previous one.
        old = await container.redis.get(f"{_RESET_USER_PREFIX}{user_id}")
        if old:
            old = old.decode() if isinstance(old, bytes) else str(old)
            await container.redis.delete(f"{_RESET_TOKEN_PREFIX}{old}")
        await container.redis.set(
            f"{_RESET_TOKEN_PREFIX}{token_hash}",
            json.dumps({"uid": user_id, "ph": fingerprint}),
            ex=_RESET_TTL,
        )
        await container.redis.set(f"{_RESET_USER_PREFIX}{user_id}", token_hash, ex=_RESET_TTL)
        # In the fragment: never sent to the server, so it can't land in access logs / Referer.
        link = f"{cabinet}#reset={token}"
        text = (
            "Здравствуйте!\n\n"
            "Кто-то (возможно, вы) запросил смену пароля для входа в личный кабинет.\n"
            f"Чтобы задать новый пароль, перейдите по ссылке:\n{link}\n\n"
            "Ссылка действует 1 час и сработает один раз. Если вы не запрашивали смену "
            "пароля — просто проигнорируйте это письмо, пароль останется прежним."
        )
        mailer = await container.build_mailer()
        await mailer.send(
            to,
            "Восстановление пароля",
            text,
            html=render_html(text, brand=brand, button=("Задать новый пароль", link)),
        )
    except Exception as exc:  # background: nobody to report to but the log
        log.warning("password reset mail failed", error=str(exc))


@router.post("/password/forgot")
async def password_forgot(
    body: PasswordForgotIn,
    request: Request,
    background: BackgroundTasks,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Step 1 of «Забыли пароль?»: always the same «ok» for any address."""
    await _require_web_enabled(container)
    await _rate_limit(container, "pw_forgot", _client_ip(request), limit=10, window=3600)
    mailer = await container.build_mailer()
    if not mailer.configured:  # independent of the address — reveals nothing about it
        raise HTTPException(503, "почта не настроена — обратитесь к владельцу сервиса")
    # Per-address caps answer the same «ok» and just send nothing: a 429 here would differ
    # by address, and the caps exist to stop someone's mailbox being flooded.
    email = str(body.email)
    hourly = await _over_limit(container, "pw_forgot_email", email, limit=3, window=3600)
    daily = await _over_limit(container, "pw_forgot_email_day", email, limit=10, window=86400)
    if not (hourly or daily):
        background.add_task(_send_reset_link, container, email)
    return {"ok": True}


class PasswordResetIn(BaseModel):
    token: str = Field(..., min_length=16, max_length=128)
    password: str = Field(..., min_length=8, max_length=128)  # same rule as RegisterIn


@router.post("/password/reset")
async def password_reset(
    body: PasswordResetIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Step 2: spend the link, set the new password, end every other cabinet session."""
    import json

    from sqlalchemy import update

    await _require_web_enabled(container)
    await _rate_limit(container, "pw_reset", _client_ip(request), limit=20, window=3600)
    # GETDEL = atomic single use: two concurrent requests with one link can't both pass.
    raw = await container.redis.getdel(f"{_RESET_TOKEN_PREFIX}{_hash(body.token)}")
    if raw is None:
        raise HTTPException(400, _RESET_INVALID)
    data = json.loads(raw)
    user_id = int(data["uid"])
    now = dt.datetime.now(dt.UTC)
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(400, _RESET_INVALID)
        if user.role.is_staff:  # same guard as login_auto: e-mail alone never unlocks staff
            raise HTTPException(403, "not allowed for staff accounts")
        _ensure_active(user)
        if not secrets.compare_digest(_password_fingerprint(user), str(data.get("ph") or "")):
            raise HTTPException(400, _RESET_INVALID)  # password changed since the link was sent
        # Compare-and-set on the old hash: of two links racing for one account (issued just
        # before each other) only one can apply — the loser matches no row.
        old_hash, new_hash = user.password_hash, hash_password(body.password)
        result = await uow.session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.password_hash.is_(None)
                if old_hash is None
                else User.password_hash == old_hash,
            )
            .values(password_hash=new_hash)
            .execution_options(synchronize_session=False)
        )
        if (cast("CursorResult[Any]", result).rowcount or 0) == 0:
            raise HTTPException(400, _RESET_INVALID)
        user.password_hash = new_hash
        user.email_verified = True  # following the link proved the mailbox is theirs
        user.notification_settings = {
            k: v
            for k, v in (user.notification_settings or {}).items()
            if k not in ("verify_token", "verify_expires")
        }
        # Guest flag off, every older session / auto-login token / link code dead, refresh
        # tokens revoked. Access JWTs issued before ``now`` are refused by web_user_from_bearer.
        await _claim_mailbox(uow, user, now)
        await uow.commit()
    await container.redis.delete(f"{_RESET_USER_PREFIX}{user_id}")
    await container.redis.delete(f"link_email:{user_id}")  # a pending «Привязать почту» too
    return await _auth_response(container, user, device=request.headers.get("user-agent"))


class PasswordPreviewIn(BaseModel):
    token: str = Field(..., min_length=16, max_length=128)


@router.post("/password/reset/preview")
async def password_reset_preview(
    body: PasswordPreviewIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Masked e-mail of the account a reset link is for («Задать пароль для a***@…?»).
    Only reads — the link stays usable. POST so the token never lands in access logs."""
    import json

    await _require_web_enabled(container)
    await _rate_limit(container, "pw_preview", _client_ip(request), limit=30, window=3600)
    raw = await container.redis.get(f"{_RESET_TOKEN_PREFIX}{_hash(body.token)}")
    if raw is None:
        raise HTTPException(400, _RESET_INVALID)
    async with container.uow() as uow:
        user = await uow.users.get(int(json.loads(raw)["uid"]))
    if user is None:
        raise HTTPException(400, _RESET_INVALID)
    return {"email": _mask_email(user.email)}


# --- «Войти через Telegram» (deep-link + confirm in the bot) -----------------

_TG_LOGIN_TTL = 300


@router.post("/telegram/start")
async def telegram_login_start(
    request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Mint a one-time login code and hand back the bot deep link.

    The person opens the bot, taps «Да, это я» there, and the site's poll exchanges
    the code for a session. Proof of identity is control of the Telegram account —
    stronger than a password, no extra BotFather setup needed.
    """
    import json as _json

    from src.application.services.account_link import TG_WEBLOGIN_PREFIX

    await _require_web_enabled(container)
    await _rate_limit(container, "tg_login", _client_ip(request), limit=30, window=3600)
    async with container.uow() as uow:
        bot_username = str(await container.bot_config.value(uow, "BOT_USERNAME") or "")
    if not bot_username:
        raise HTTPException(503, "бот не настроен")
    code = secrets.token_urlsafe(24)
    await container.redis.set(
        f"{TG_WEBLOGIN_PREFIX}{code}", _json.dumps({"status": "pending"}), ex=_TG_LOGIN_TTL
    )
    return {
        "code": code,
        "url": f"https://t.me/{bot_username}?start=weblogin_{code}",
        "expires_in": _TG_LOGIN_TTL,
    }


class TelegramPollIn(BaseModel):
    code: str = Field(..., min_length=8, max_length=64)


@router.post("/telegram/poll")
async def telegram_login_poll(
    body: TelegramPollIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """The site polls until the bot marks the code confirmed, then gets a session."""
    import json as _json

    from src.application.services.account_link import TG_WEBLOGIN_PREFIX

    await _rate_limit(container, "tg_poll", _client_ip(request), limit=300, window=300)
    key = f"{TG_WEBLOGIN_PREFIX}{body.code}"
    raw = await container.redis.get(key)
    if raw is None:
        raise HTTPException(400, "код устарел — начни вход заново")
    data = _json.loads(raw)
    if data.get("status") != "ok":
        return {"pending": True}
    await container.redis.delete(key)  # single use: one confirmed code -> one session
    async with container.uow() as uow:
        user = await uow.users.get(int(data["user_id"]))
    if user is None:
        raise HTTPException(401, "user gone")
    return await _auth_response(container, user, device=request.headers.get("user-agent"))


# --- OAuth (Google / Yandex / VK ID) ----------------------------------------


def _redirect_uri(cabinet_url: str) -> str:
    """Where the provider sends the browser back — must match the console entry byte-for-byte.

    The web cabinet SPA (the page that reads ``?code&state`` on load) is mounted at ``/web``;
    ``/`` is the public landing, which has no OAuth handling at all. CABINET_URL is autofilled
    with the BARE domain, so the callback used to land on the landing page: the code was
    dropped, nothing happened, and the operator saw «авторизация не проходит» with no error.
    Append ``/web`` for a bare host; an explicit path the operator pinned themselves is kept.
    """
    url = cabinet_url.strip().rstrip("/")
    path = urlparse(url).path
    return f"{url}/web" if path in ("", "/") else url


async def _oauth_provider(container: AppContainer, name: str):  # type: ignore[no-untyped-def]
    from src.infrastructure.services.oauth import build_provider

    async with container.uow() as uow:
        cfg = container.bot_config
        cid = str(await cfg.value(uow, f"OAUTH_{name.upper()}_CLIENT_ID") or "")
        sec = str(await cfg.value(uow, f"OAUTH_{name.upper()}_CLIENT_SECRET") or "")
        cabinet_url = str(await cfg.value(uow, "CABINET_URL") or "")
    return build_provider(name, cid, sec), cabinet_url


@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    request: Request,
    link: bool = False,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Start an OAuth flow. With ``?link=1`` + an authenticated session the callback
    ATTACHES the provider account to the current user instead of logging in."""
    await _require_web_enabled(container)
    from src.infrastructure.services.oauth import make_pkce_pair, save_state

    prov, cabinet_url = await _oauth_provider(container, provider)
    if prov is None or not cabinet_url:
        raise HTTPException(400, "provider not configured")
    payload: dict[str, Any] = {"provider": provider}
    if link:
        from src.web.routes.cabinet import cabinet_user

        user = await cabinet_user(request, container)
        if is_guest_account(user):  # an unclaimed guest account binds nothing (pre-claim)
            raise HTTPException(403, GUEST_CLAIM_FIRST)
        payload["link_user_id"] = user.id
        payload["link_issued"] = int(dt.datetime.now(dt.UTC).timestamp())
    challenge = None
    if prov.needs_pkce:
        verifier, challenge = make_pkce_pair()
        payload["verifier"] = verifier
    state = await save_state(container.redis, payload)
    return {
        "authorize_url": prov.authorize_url(_redirect_uri(cabinet_url), state, challenge),
        "state": state,
    }


class OAuthCallbackIn(BaseModel):
    provider: str = Field(..., min_length=2, max_length=32)
    code: str = Field(..., min_length=4, max_length=2048)
    state: str = Field(..., min_length=8, max_length=128)
    device_id: str | None = Field(None, max_length=256)  # VK ID appends it to the callback


async def _oauth_login(container: AppContainer, provider: str, info: Any) -> User:
    """Resolve an OAuth identity to a local user: identity row first, verified e-mail
    second, a brand-new account last (VK often has no e-mail — identity is the key)."""
    from src.infrastructure.database.models.linked_account import LinkedAccount

    async with container.uow() as uow:
        ident = await uow.linked_accounts.get_identity(provider, info.provider_id)
        if ident is not None:
            user = await uow.users.get(ident.user_id)
            if user is None:
                raise HTTPException(401, "user gone")
            if info.email and ident.email != info.email:
                ident.email = info.email
                await uow.commit()
            return user

        user = None
        if info.email and info.email_verified:
            user = await uow.users.get_by_email(info.email)
            if user is not None and is_guest_account(user):
                # The provider vouches for the mailbox: its owner claims the guest account a
                # purchase created for this address. Whoever bought it (and holds its auto-login
                # token) is shut out; nobody ever knew its random password.
                user.email_verified = True
                await _claim_mailbox(uow, user, dt.datetime.now(dt.UTC))
            elif user is not None and not user.email_verified:
                # a never-confirmed local account must not be taken over via OAuth
                raise HTTPException(409, "email exists but is unverified — log in and link instead")
        if user is None:
            user = User(
                auth_type=AuthType.OAUTH,
                email=info.email if (info.email and info.email_verified) else None,
                email_verified=bool(info.email and info.email_verified),
                first_name=(info.name or "")[:128] or None,
                referral_code=generate_referral_code(),
                currency=Currency.RUB,
            )
            await uow.users.add(user)
            await uow.session.flush()
        uow.session.add(
            LinkedAccount(
                user_id=user.id,
                provider=provider,
                external_id=info.provider_id,
                email=info.email,
                display_name=(info.name or "")[:128] or None,
            )
        )
        try:
            await uow.commit()
        except IntegrityError as exc:  # lost a concurrent-login race for the same identity
            raise HTTPException(409, "try again") from exc
        user_id = user.id
    async with container.uow() as uow:
        fresh = await uow.users.get(user_id)
    if fresh is None:
        raise HTTPException(500, "oauth login failed")
    return fresh


async def _oauth_link(
    container: AppContainer, provider: str, info: Any, user_id: int, *, issued: int = 0
) -> dict[str, Any]:
    """Attach the provider identity to an already-authenticated account. ``issued`` — when the
    link flow was started; a password reset since then voids it."""
    from src.infrastructure.database.models.linked_account import LinkedAccount

    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(401, "user gone")
        if is_guest_account(user):
            raise HTTPException(403, GUEST_CLAIM_FIRST)
        if issued < password_changed_at(user):
            raise HTTPException(401, "invalid or expired state")
        ident = await uow.linked_accounts.get_identity(provider, info.provider_id)
        if ident is not None:
            if ident.user_id != user.id:
                raise HTTPException(409, "этот аккаунт уже привязан к другому пользователю")
            return {"ok": True, "linked": provider, "already": True}
        uow.session.add(
            LinkedAccount(
                user_id=user.id,
                provider=provider,
                external_id=info.provider_id,
                email=info.email,
                display_name=(info.name or "")[:128] or None,
            )
        )
        # Bonus: an account without an e-mail inherits the provider's verified one.
        if (
            not user.email
            and info.email
            and info.email_verified
            and await uow.users.get_by_email(info.email) is None
        ):
            user.email = info.email
            user.email_verified = True
        try:
            await uow.commit()
        except IntegrityError as exc:
            raise HTTPException(409, "этот аккаунт уже привязан к другому пользователю") from exc
    return {"ok": True, "linked": provider}


@router.post("/oauth/callback")
async def oauth_callback(
    body: OAuthCallbackIn, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    await _require_web_enabled(container)
    from src.infrastructure.services.oauth import consume_state

    saved = await consume_state(container.redis, body.state)  # single-use (anti-replay)
    if saved is None or saved.get("provider") != body.provider:
        raise HTTPException(401, "invalid or expired state")
    prov, cabinet_url = await _oauth_provider(container, body.provider)
    if prov is None or not cabinet_url:
        raise HTTPException(400, "provider not configured")
    try:
        info = await prov.fetch_user(
            body.code,
            _redirect_uri(cabinet_url),
            code_verifier=saved.get("verifier"),
            device_id=body.device_id,
        )
    except Exception as exc:
        raise HTTPException(400, f"oauth exchange failed: {exc}") from exc

    link_user_id = saved.get("link_user_id")
    if link_user_id:
        return await _oauth_link(
            container,
            body.provider,
            info,
            int(link_user_id),
            issued=int(saved.get("link_issued") or 0),
        )
    user = await _oauth_login(container, body.provider, info)
    return await _auth_response(container, user, device=request.headers.get("user-agent"))
