"""Admin: users list/detail + drawer actions (screen 02)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import re
from dataclasses import replace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import ColumnElement, Select, String, and_, func, or_, select, update
from sqlalchemy import delete as sa_delete

from src.application.services import clients
from src.application.services.ids import generate_referral_code
from src.application.services.panel_guard import (
    SERVICE_ACCOUNT_REFUSAL,
    PanelServiceAccountError,
    is_service_username,
    parse_patterns,
)
from src.core.constants import BYTES_PER_GB, UNLIMITED_EXPIRE_DAYS
from src.core.enums import (
    AuthType,
    Role,
    SubscriptionStatus,
    TransactionStatus,
    TransactionType,
    UserStatus,
)
from src.core.exceptions import DomainError, RemnawaveError
from src.core.logging import get_logger
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import OkOut, Page, audit, iso
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/users")
log = get_logger(__name__)


def _like_escape(q: str) -> str:
    """Escape LIKE wildcards in user input so «%» doesn't match everything."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_SUB_FILTERS: dict[str, tuple[SubscriptionStatus, ...]] = {
    "active": (SubscriptionStatus.ACTIVE, SubscriptionStatus.LIMITED),
    "trial": (SubscriptionStatus.TRIAL,),
    "expired": (SubscriptionStatus.EXPIRED, SubscriptionStatus.DISABLED),
}


def _list_stmt(status_filter: str, q: str, *, private: bool = False) -> Select[Any]:
    stmt = (
        select(User, Subscription)
        .outerjoin(Subscription, Subscription.id == User.current_subscription_id)
        .where(User.role != Role.SYSTEM)
    )
    # A «working» subscription whose expiry has passed counts as expired (its stored status
    # goes stale when its panel user is deleted — nothing tells the bot); a grace window still
    # counts as working. Same rule as clients.effective_status.
    now = dt.datetime.now(dt.UTC)
    in_grace = and_(Subscription.grace_until.is_not(None), Subscription.grace_until > now)
    not_ended = or_(Subscription.expire_at.is_(None), Subscription.expire_at > now, in_grace)
    if status_filter == "blocked":
        stmt = stmt.where(User.status == UserStatus.BLOCKED)
    elif status_filter == "expired":
        stmt = stmt.where(
            User.status == UserStatus.ACTIVE,
            or_(
                Subscription.status.in_(_SUB_FILTERS["expired"]),
                and_(
                    Subscription.status.in_(_SUB_FILTERS["active"] + _SUB_FILTERS["trial"]),
                    ~not_ended,
                ),
            ),
        )
    elif status_filter in _SUB_FILTERS:
        stmt = stmt.where(
            User.status == UserStatus.ACTIVE,
            Subscription.status.in_(_SUB_FILTERS[status_filter]),
            not_ended,
        )
    if q:
        q = q.strip()
        needle = f"%{_like_escape(q.lstrip('@').lower())}%"
        clauses: list[ColumnElement[bool]] = [
            func.lower(func.coalesce(User.username, "")).like(needle),
            func.lower(func.coalesce(User.first_name, "")).like(needle),
            func.lower(func.coalesce(User.last_name, "")).like(needle),
        ]
        if not private:
            # the subscription code — also what panel usernames «sub_<code>» are made of; for
            # imported subs it is the link token, so keys / demo can't probe by it
            clauses.append(
                func.lower(func.coalesce(Subscription.short_id, "")).like(
                    f"%{_like_escape(q.lower().removeprefix('sub_'))}%"
                )
            )
        # People are looked up by every number the admin can see on them: the bot's «id75»,
        # the Remnawave user number, the Telegram ID (or its start), and the Telegram ID inside
        # a panel username like «user_5072755730».
        # `isascii()` guards int(): unicode digits like '²' pass str.isdigit() but int('²')
        # raises ValueError, which would 500 the whole user search.
        low = q.lower()
        digits = low.removeprefix("id").removeprefix("user_").removeprefix("tg:")
        if digits.isascii() and digits.isdigit() and len(digits) <= 18:
            number = int(digits)
            clauses.append(User.telegram_id == number)
            if len(digits) >= 4:
                clauses.append(func.cast(User.telegram_id, String).like(f"{digits}%"))
            if not low.startswith(("user_", "tg:")) and number < 2**31:
                clauses.append(User.id == number)
                if not low.startswith("id"):
                    clauses.append(Subscription.remnawave_id == number)
        stmt = stmt.where(or_(*clauses))
    return stmt


def _user_status(user: User, sub: Subscription | None) -> str:
    """The legacy five states of this list (the «Клиенты» list has finer ones)."""
    state = clients.effective_status(
        user_status=user.status,
        sub_status=sub.status if sub is not None else None,
        expire_at=sub.expire_at if sub is not None else None,
        grace_until=sub.grace_until if sub is not None else None,
        now=dt.datetime.now(dt.UTC),
    )
    return {"grace": "active", "limited": "active", "disabled": "expired"}.get(state, state)


async def _service_patterns(container: AppContainer, uow: Any) -> tuple[str, ...]:
    return parse_patterns(
        str(await container.bot_config.value(uow, "PANEL_SERVICE_USERNAMES") or "")
    )


async def _is_service_user(container: AppContainer, uow: Any, user: User) -> bool:
    """One of MultiScript's service accounts (by the panel username the bot recorded)."""
    patterns = await _service_patterns(container, uow)
    if not patterns:
        return False
    subs = await uow.subscriptions.list(user_id=user.id)
    return any(is_service_username(s.panel_username, patterns) for s in subs)


async def _refuse_service(container: AppContainer, uow: Any, user: User) -> None:
    """Service accounts are never changed through the bot — 403 before any panel call (the
    panel guard refuses the write itself too; this only makes the answer clear)."""
    if await _is_service_user(container, uow, user):
        raise HTTPException(403, SERVICE_ACCOUNT_REFUSAL)


def _panel_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PanelServiceAccountError):
        return HTTPException(403, SERVICE_ACCOUNT_REFUSAL)
    return HTTPException(502, f"panel error: {exc}")


def _restricted(identity: AdminIdentity) -> bool:
    """A read-only AI key or a demo (PREVIEW) session: no personal data beyond what the key
    filter lets through, no forced panel reads, no search by e-mail / link code (an oracle)."""
    return identity.api_key_id is not None or identity.role is Role.PREVIEW


def _mask_private(data: dict[str, Any]) -> dict[str, Any]:
    """The same masking the key filter applies (e-mail, link code, subscription link) — for
    demo sessions, which don't go through that filter."""
    from src.web.routes.admin.api_key_auth import mask_personal

    masked: dict[str, Any] = mask_personal(data)
    return masked


def _touched(container: AppContainer) -> None:
    """An admin action changed the panel — the «Клиенты» list re-reads it next time."""
    container.panel_snapshot.invalidate()


def _row(user: User, sub: Subscription | None) -> dict[str, Any]:
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "name": " ".join(filter(None, [user.first_name, user.last_name])) or None,
        "status": _user_status(user, sub),
        "role": user.role.name,
        "balance_minor": user.balance_minor,
        "currency": user.currency.value,
        "plan_name": (sub.plan_snapshot or {}).get("name") if sub else None,
        "expire_at": iso(sub.expire_at) if sub else None,
        "traffic_used_bytes": sub.traffic_used_bytes if sub else 0,
        "traffic_limit_bytes": sub.traffic_limit_bytes if sub else 0,
        "device_limit": sub.device_limit if sub else None,
        "current_subscription_id": user.current_subscription_id,
        "created_at": iso(user.created_at),
        "last_seen_at": iso(user.updated_at),
    }


@router.get("", response_model=Page)
async def list_users(
    q: str = Query("", max_length=64),
    status: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> Page:
    async with container.uow() as uow:
        stmt = _list_stmt(status, q, private=_restricted(identity))
        total = int(
            await uow.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        )
        rows = (
            await uow.session.execute(stmt.order_by(User.id.desc()).limit(limit).offset(offset))
        ).all()
        items = [_row(u, s) for u, s in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)


class UserCreateIn(BaseModel):
    """Manual onboarding — e.g. a technician registering a cash customer for a router install
    who has no bot interaction (no /start) yet, or an email-only contact. Not the normal
    signup path (that's /start or the web cabinet's own registration); this is admin-only."""

    telegram_id: int | None = None
    username: str | None = Field(None, max_length=64)
    first_name: str | None = Field(None, max_length=128)
    email: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _needs_an_identity(self) -> UserCreateIn:
        if not self.telegram_id and not (self.email or "").strip():
            raise ValueError("provide telegram_id or email")
        return self


@router.post("")
async def create_user(
    body: UserCreateIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    email = body.email.strip().lower() if body.email else None
    async with container.uow() as uow:
        if body.telegram_id and await uow.users.find_one(telegram_id=body.telegram_id):
            raise HTTPException(409, "a user with this telegram_id already exists")
        if email and await uow.users.find_one(email=email):
            raise HTTPException(409, "a user with this email already exists")
        user = User(
            telegram_id=body.telegram_id,
            username=(body.username or "").lstrip("@").strip() or None,
            first_name=(body.first_name or "").strip() or None,
            email=email,
            auth_type=AuthType.TELEGRAM if body.telegram_id else AuthType.EMAIL,
            referral_code=generate_referral_code(),
        )
        await uow.users.add(user)
        await audit(
            uow,
            identity,
            "user.create_manual",
            f"user:{user.id}",
            telegram_id=body.telegram_id,
            email=email,
        )
        await uow.commit()
        row = _row(user, None)
    return row


class CountersOut(BaseModel):
    all: int
    active: int
    trial: int
    expired: int
    blocked: int


@router.get("/counters", response_model=CountersOut)
async def counters(container: AppContainer = Depends(get_container)) -> CountersOut:
    async with container.uow() as uow:
        out: dict[str, int] = {}
        for name in ("all", "active", "trial", "expired", "blocked"):
            stmt = _list_stmt(name, "")
            out[name] = int(
                await uow.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            )
    return CountersOut(**out)


# --- «Клиенты»: the list with chips, tiles and panel columns ------------------------------
# Declared before /{user_id} (that int path would swallow «clients» / «manual» with a 422).

_EXPORT_ROLES = (Role.OWNER, Role.DEV)


async def _client_rows(
    container: AppContainer, *, refresh: bool
) -> tuple[clients.PanelSnapshot, list[dict[str, Any]], dt.datetime]:
    snap = await container.panel_snapshot.get(force=refresh)
    now = dt.datetime.now(dt.UTC)
    async with container.uow() as uow:
        patterns = await _service_patterns(container, uow)
        data = await clients.load_rows(uow, snap, service_patterns=patterns, now=now)
        if data.panel_names:
            # Remember the panel's name for each subscription: how service accounts are known
            # without the panel, and what «Имя в панели» shows while the panel is down. A write
            # on a GET (also for keys / demo) by design: it only mirrors the panel's own names.
            for sub_id, name in data.panel_names.items():
                await uow.session.execute(
                    update(Subscription)
                    .where(Subscription.id == sub_id)
                    .values(panel_username=name)
                )
            await uow.commit()
    return snap, data.rows, now


@router.get("/clients")
async def list_clients(
    q: str = Query("", max_length=64),
    chip: str = Query("all", max_length=32),
    sort: str = Query("created", max_length=16),
    desc: bool = Query(True),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    refresh: bool = Query(False),
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """The customer list: bot data + ONE cached panel snapshot (see services/clients.py).
    ``counters`` — every chip (service accounts only under «service»); ``panel`` — whether the
    panel answered and when. ``refresh`` re-reads the panel (not for read-only keys)."""
    private = _restricted(identity)
    snap, rows, now = await _client_rows(container, refresh=refresh and not private)
    async with container.uow() as uow:
        patterns = await _service_patterns(container, uow)
    picked = clients.select_rows(
        rows, chip=chip, q=q, sort=sort, desc=desc, now=now, private=private
    )
    counts = clients.counters(rows, now)
    items = picked[offset : offset + limit]
    if private:
        items = [_mask_private(r) for r in items]
    return {
        "items": items,
        "total": len(picked),
        "limit": limit,
        "offset": offset,
        "counters": counts,
        # the five tiles as MultiScript counts them (panel users); null without panel data
        "tiles": clients.panel_tiles(snap, patterns, now),
        "panel": {
            "ok": snap.error is None,
            "error": snap.error,
            "devices_error": snap.devices_error,
            "fetched_at": iso(snap.fetched_at),
        },
        "can_export": identity.api_key_id is None and identity.role in _EXPORT_ROLES,
    }


@router.get("/clients/export")
async def export_clients(
    q: str = Query("", max_length=64),
    chip: str = Query("all", max_length=32),
    sort: str = Query("created", max_length=16),
    desc: bool = Query(True),
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> Response:
    """The current selection as CSV. OWNER / DEV only, closed to read-only keys."""
    if identity.api_key_id is not None or identity.role not in _EXPORT_ROLES:
        raise HTTPException(403, "только владелец или разработчик")
    _, rows, now = await _client_rows(container, refresh=False)
    picked = clients.select_rows(rows, chip=chip, q=q, sort=sort, desc=desc, now=now)
    async with container.uow() as uow:
        await audit(uow, identity, "users.export", None, chip=chip, q=q, rows=len(picked))
        await uow.commit()
    name = f"clients-{now:%Y%m%d-%H%M}.csv"
    return Response(
        content=clients.to_csv(picked),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# Remnawave's rule for a user tag: upper-case latin letters, digits and «_», up to 16 chars.
_TAG = re.compile(r"^[A-Z0-9_]{1,16}$")
_TG_USERNAME = re.compile(r"^[A-Za-z0-9_]{4,32}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # the cabinet's rule (cabinet_auth)
_LAST_DAY = dt.date(2099, 12, 31)  # Remnawave's «forever»; later dates are refused
_TG_IN_NAME = re.compile(r"tg:\s*\d+", re.IGNORECASE)


def _clean_tag(value: str | None) -> str | None:
    if value is None:
        return None
    tag = value.strip().upper()
    if tag and not _TAG.match(tag):
        raise ValueError("метка: только латинские буквы A-Z, цифры и _, до 16 символов")
    return tag


class ManualClientIn(BaseModel):
    """«+ Добавить клиента»: the bot user, their subscription and the panel user in one go."""

    name: str = Field(..., min_length=1, max_length=128)
    telegram: str | None = Field(None, max_length=64)  # numeric ID or @username
    email: str | None = Field(None, max_length=255)
    tag: str | None = Field(None, max_length=16)
    term: Literal["days", "until", "forever", "plan"] = "days"
    days: int | None = Field(None, ge=1, le=3650)
    until: dt.date | None = None
    plan_id: int | None = None
    traffic_gb: int | None = Field(None, ge=0, le=1_000_000)  # 0 = unlimited
    device_limit: int | None = Field(None, ge=0, le=100)  # None = default, 0 = unlimited
    internal_squads: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("укажите имя")
        return value

    @field_validator("tag")
    @classmethod
    def _tag(cls, value: str | None) -> str | None:
        return _clean_tag(value) or None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        value = (value or "").strip().lower()
        if value and not _EMAIL.match(value):
            raise ValueError("некорректный e-mail")
        return value or None

    @model_validator(mode="after")
    def _term(self) -> ManualClientIn:
        if self.term in ("days", "plan") and self.days is None:
            raise ValueError("укажите срок в днях")
        if self.term == "plan" and self.plan_id is None:
            raise ValueError("выберите тариф")
        if self.term == "until":
            if self.until is None:
                raise ValueError("укажите дату окончания")
            if self.until < dt.datetime.now(dt.UTC).date():
                raise ValueError("дата окончания уже прошла")
            if self.until > _LAST_DAY:
                raise ValueError("дата окончания — не позже 31.12.2099")
        return self

    def panel_description(self, telegram_id: int | None) -> str:
        """The name for the panel's description. A «tg:<id>» typed into the name is cut out:
        tools read the Telegram ID from the description, and it must be the real one."""
        name = " ".join(_TG_IN_NAME.sub(" ", self.name).split()) or "клиент"
        return (f"tg:{telegram_id} {name}" if telegram_id is not None else name)[:200]

    def telegram_ref(self) -> tuple[int | None, str | None]:
        """(telegram_id, username) from the «Telegram» field."""
        raw = (self.telegram or "").strip()
        if not raw:
            return None, None
        if raw.isascii() and raw.isdigit():
            if len(raw) > 18:
                raise HTTPException(422, "Telegram ID слишком длинный")
            return int(raw), None
        handle = raw.removeprefix("https://t.me/").removeprefix("t.me/").lstrip("@")
        if not _TG_USERNAME.match(handle):
            raise HTTPException(422, "Telegram: числовой ID или @username (латиница, 4-32)")
        return None, handle


def _exists(message: str, user_id: int) -> HTTPException:
    return HTTPException(409, {"message": message, "user_id": user_id})


@router.post("/manual")
async def create_client_manual(
    body: ManualClientIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Add a customer by hand — cash customers, relatives: a bot user (Telegram and e-mail
    optional), a subscription and its panel user, panel-first like every grant. Without a plan
    the expiry/limits are the admin's; with one it is the /grant path (its limits, squads)."""
    from src.application.dto.pricing import PurchaseRequest
    from src.core.enums import PurchaseType

    tg_id, tg_username = body.telegram_ref()
    email = body.email
    now = dt.datetime.now(dt.UTC)
    async with container.uow() as uow:
        if tg_id is not None and (found := await uow.users.find_one(telegram_id=tg_id)):
            raise _exists("Такой Telegram ID уже есть в боте", found.id)
        if tg_username is not None:
            found = await uow.session.scalar(
                select(User).where(func.lower(User.username) == tg_username.lower()).limit(1)
            )
            if found is not None:
                raise _exists(f"Клиент @{tg_username} уже есть в боте", found.id)
        if email and (found := await uow.users.find_one(email=email)):
            raise _exists("Такой e-mail уже есть в боте", found.id)
        plan = None
        if body.term == "plan":
            plan = await uow.plans.get(body.plan_id or 0)
            if plan is None:
                raise HTTPException(404, "plan not found")
        known = {str(s.squad_uuid) for s in await uow.server_squads.list()}
        squads = tuple(dict.fromkeys(s for s in body.internal_squads if s in known))
        if body.internal_squads and not squads:
            raise HTTPException(422, "такого сквада нет — обновите список серверов")

        user = User(
            telegram_id=tg_id,
            username=tg_username,
            first_name=body.name,
            email=email,
            auth_type=(
                AuthType.TELEGRAM
                if tg_id is not None
                else AuthType.EMAIL
                if email
                else AuthType.MANUAL
            ),
            referral_code=generate_referral_code(),
        )
        await uow.users.add(user)
        # The panel user carries the name — that's how the owner finds them in Remnawave too.
        description = body.panel_description(tg_id)
        traffic = (body.traffic_gb or 0) * BYTES_PER_GB
        try:
            if plan is not None:
                assert body.days is not None
                req = PurchaseRequest(
                    user_id=user.id,
                    plan_id=plan.id,
                    duration_days=body.days,
                    currency=user.currency,
                    purchase_type=PurchaseType.NEW,
                    internal_squads=squads,
                    traffic_limit_bytes=traffic if body.traffic_gb is not None else None,
                    device_limit=body.device_limit,
                )
                sub = await container.subscriptions.grant(
                    uow,
                    user=user,
                    plan=plan,
                    req=req,
                    mark_paid=False,
                    description=description,
                    tag=body.tag,
                )
            else:
                if body.term == "until":
                    assert body.until is not None
                    expire_at = dt.datetime.combine(body.until, dt.time(23, 59, 59), tzinfo=dt.UTC)
                elif body.term == "forever":
                    expire_at = now + dt.timedelta(days=UNLIMITED_EXPIRE_DAYS)
                else:
                    assert body.days is not None
                    expire_at = now + dt.timedelta(days=body.days)
                device_limit = body.device_limit
                if device_limit is None:
                    device_limit = int(
                        await container.bot_config.value(uow, "DEFAULT_DEVICE_LIMIT") or 0
                    )
                sub = await container.subscriptions.grant_manual(
                    uow,
                    user=user,
                    expire_at=expire_at,
                    traffic_limit_bytes=traffic,
                    device_limit=device_limit,
                    internal_squads=squads,
                    description=description,
                    tag=body.tag,
                )
        except RemnawaveError as exc:  # nothing is saved: the uow rolls back
            raise _panel_error(exc) from exc
        except DomainError as exc:
            raise HTTPException(400, str(exc)) from exc
        panel_ref = sub.panel_ref  # read now: a failed commit expires the row
        try:
            await _audit_manual(uow, identity, body, user, sub, tg_id, tg_username, plan)
            await uow.commit()
        except Exception:
            # The panel user exists but the bot's record doesn't: remove it (best effort,
            # logged) instead of leaving an orphan nobody pays for, then report the error.
            log.warning("manual client: local save failed, removing the panel user")
            if panel_ref is not None:
                await container.remnawave.discard(panel_ref)
            raise
        out = {
            "user_id": user.id,
            "subscription_id": sub.id,
            "short_id": sub.short_id,
            "panel_username": sub.panel_username,
            "subscription_url": sub.subscription_url,
            "expire_at": iso(sub.expire_at),
        }
    _touched(container)
    return out


async def _audit_manual(
    uow: Any,
    identity: AdminIdentity,
    body: ManualClientIn,
    user: User,
    sub: Subscription,
    tg_id: int | None,
    tg_username: str | None,
    plan: Any,
) -> None:
    await audit(
        uow,
        identity,
        "user.create_manual",
        f"user:{user.id}",
        telegram_id=tg_id,
        username=tg_username,
        email=body.email,
        term=body.term,
        plan_id=plan.id if plan is not None else None,
        days=body.days,
        until=str(body.until) if body.until else None,
        traffic_gb=body.traffic_gb,
        device_limit=sub.device_limit,
        tag=body.tag,
    )


# --- «Синхронизация с панелью»: adopt panel-only users ------------------------------------
# Declared before /{user_id}: that int path would otherwise swallow «panel-sync» with a 422.


@router.get("/panel-sync")
async def panel_sync_preview(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Who is in Remnawave but not in the bot, and panel users two bot subscriptions point at.
    Read-only."""
    from src.application.services.panel_import import find_duplicates, find_panel_only

    try:
        panel_users = await container.remnawave_client.list_users()
    except RemnawaveError as exc:
        raise HTTPException(502, f"панель недоступна: {exc}") from exc
    async with container.uow() as uow:
        patterns = await _service_patterns(container, uow)
        missing = await find_panel_only(uow, panel_users, service_patterns=patterns)
        dupes = await find_duplicates(uow, panel_users)
        bot_total = await uow.users.count()
        # Bot users the panel has no account for: started the bot but never bought, or a
        # router placeholder still waiting for its QR — why «bot» can exceed «panel».
        no_sub = await uow.session.scalar(
            select(func.count()).select_from(User).where(User.current_subscription_id.is_(None))
        )
    return {
        "panel_total": len(panel_users),
        "panel_linked": len(panel_users) - len(missing),
        "bot_total": bot_total,
        "bot_without_subscription": int(no_sub or 0),
        "items": [m.as_dict() for m in missing],
        "duplicates": [d.as_dict() for d in dupes],
    }


@router.post("/panel-sync/repair")
async def panel_sync_repair(
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Drop the duplicate subscriptions the panel import created (see find_duplicates)."""
    from src.application.services.panel_import import find_duplicates, repair_duplicates

    try:
        panel_users = await container.remnawave_client.list_users()
    except RemnawaveError as exc:
        raise HTTPException(502, f"панель недоступна: {exc}") from exc
    async with container.uow() as uow:
        result = await repair_duplicates(uow, await find_duplicates(uow, panel_users))
        await audit(uow, identity, "users.panel_sync_repair", None, **result)
        await uow.commit()
    return result


class PanelSyncIn(BaseModel):
    keys: list[str] = Field(..., min_length=1, max_length=2000)


async def _telegram_names(container: AppContainer, user_ids: list[int]) -> int:
    """Best effort: name adopted people by their Telegram profile. Works for anyone who has
    ever opened this bot (Telegram only shares chats with the bot); the rest show as «idN»
    with their Telegram ID, like every other nameless user."""
    token = container.settings.bot.token
    if not token or not user_ids:
        return 0
    from aiogram import Bot

    named = 0
    bot = Bot(token=token)
    try:
        async with container.uow() as uow:
            for uid in user_ids[:300]:
                user = await uow.users.get(uid)
                if user is None or not user.telegram_id:
                    continue
                try:
                    chat = await bot.get_chat(user.telegram_id)
                except Exception:
                    continue
                user.username = (chat.username or "")[:64] or user.username
                user.first_name = (chat.first_name or "")[:128] or user.first_name
                user.last_name = (chat.last_name or "")[:128] or user.last_name
                named += 1
            await uow.commit()
    finally:
        await bot.session.close()
    return named


@router.post("/panel-sync")
async def panel_sync_import(
    body: PanelSyncIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Adopt the picked panel users: bot user + subscription pointing at the SAME panel user.
    The panel itself is not touched."""
    from src.application.services.panel_import import adopt, find_panel_only

    try:
        panel_users = await container.remnawave_client.list_users()
    except RemnawaveError as exc:
        raise HTTPException(502, f"панель недоступна: {exc}") from exc
    async with container.uow() as uow:
        patterns = await _service_patterns(container, uow)
        missing = await find_panel_only(uow, panel_users, service_patterns=patterns)
        summary = await adopt(uow, missing, set(body.keys))
        await audit(
            uow,
            identity,
            "users.panel_sync",
            None,
            subscriptions=summary["subscriptions"],
            users_created=summary["users_created"],
        )
        await uow.commit()
    summary["named_from_telegram"] = await _telegram_names(container, summary["new_user_ids"])
    summary.pop("new_user_ids", None)
    return summary


async def _panel_view(
    container: AppContainer, sub: Subscription | None, telegram_id: int | None
) -> dict[str, Any] | None:
    """Remnawave's side of this subscription, with what disagrees with the bot."""
    if sub is None or sub.panel_ref is None:
        return None
    ref = replace(sub.panel_ref, telegram_id=telegram_id)
    try:
        panel = await asyncio.wait_for(container.remnawave_client.get_user(ref), timeout=2.5)
    except Exception as exc:
        return {"error": f"панель не ответила: {str(exc)[:120]}"}
    if panel is None:
        return {"error": "в панели такого пользователя нет — подписка ссылается на удалённого"}
    status = panel.status or ("ACTIVE" if panel.is_enabled else "DISABLED")
    mismatch: list[str] = []
    bot_live = sub.status.is_usable
    if bot_live and status != "ACTIVE":
        mismatch.append(f"в боте подписка рабочая, а в панели {status}")
    if not bot_live and status == "ACTIVE":
        mismatch.append("в панели активен, а в боте подписка не рабочая")
    if sub.expire_at and panel.expire_at:
        bot_end = sub.expire_at if sub.expire_at.tzinfo else sub.expire_at.replace(tzinfo=dt.UTC)
        if abs((bot_end - panel.expire_at).total_seconds()) > 86400:
            mismatch.append(
                f"срок: в боте до {bot_end:%d.%m.%Y}, в панели до {panel.expire_at:%d.%m.%Y}"
            )
    if (sub.device_limit or 0) != (panel.device_limit or 0):
        mismatch.append(
            f"лимит устройств: в боте {sub.device_limit or '∞'}, в панели {panel.device_limit or '∞'}"
        )
    return {
        "username": panel.username,
        "panel_id": panel.panel_id,
        "status": status,
        "expire_at": iso(panel.expire_at),
        "device_limit": panel.device_limit,
        "traffic_used_bytes": panel.traffic_used_bytes,
        "traffic_limit_bytes": panel.traffic_limit_bytes,
        "tag": panel.tag,
        "online_at": iso(panel.online_at),
        "mismatch": mismatch,
    }


@router.get("/{user_id}")
async def user_detail(
    user_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    detail = await _user_detail(user_id, container)
    return _mask_private(detail) if identity.role is Role.PREVIEW else detail


async def _user_detail(user_id: int, container: AppContainer) -> dict[str, Any]:
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
        # The person as Remnawave has them — shown next to the bot's view so the two can be
        # compared at a glance (panel username like «user_5072755730», number, status). Also
        # the live traffic: the stored number only moves on webhooks / the customer's screens.
        panel_info = await _panel_view(container, sub, user.telegram_id)
        if sub is not None and panel_info is not None and "traffic_used_bytes" in panel_info:
            sub.traffic_used_bytes = int(panel_info["traffic_used_bytes"])
            await uow.commit()
        txs = await uow.transactions.list_recent(user_id, limit=10)
        invited = await uow.users.count(referred_by_id=user_id)
        earned_minor = await uow.referral_earnings.total_minor(user_id)

        detail = _row(user, sub)
        detail["panel"] = panel_info
        detail["is_service"] = await _is_service_user(container, uow, user)
        detail["email"] = user.email
        detail["grace_until"] = iso(sub.grace_until) if sub is not None else None
        detail.update(
            {
                "referral_code": user.referral_code,
                "referral_invited": invited,
                "referral_earned_minor": earned_minor,
                "is_trial_available": user.is_trial_available,
                "personal_discount_pct": user.personal_discount_pct,
                "purchase_discount_pct": user.purchase_discount_pct,
                "subscription": None,
                "transactions": [
                    {
                        "id": t.id,
                        "type": t.type.value,
                        "status": t.status.value,
                        "amount_minor": t.amount_minor,
                        "currency": t.currency.value,
                        "gateway": t.gateway_type.value if t.gateway_type else None,
                        "created_at": iso(t.created_at),
                    }
                    for t in sorted(txs, key=lambda t: t.id, reverse=True)
                ],
            }
        )
        if sub is not None:
            detail["subscription"] = {
                "id": sub.id,
                "status": sub.status.value,
                "is_trial": sub.is_trial,
                "short_id": sub.short_id,
                "remnawave_uuid": str(sub.remnawave_uuid) if sub.remnawave_uuid else None,
                "remnawave_id": sub.remnawave_id,
                "plan_snapshot": sub.plan_snapshot,
                "expire_at": iso(sub.expire_at),
                "traffic_used_bytes": sub.traffic_used_bytes,
                "traffic_limit_bytes": sub.traffic_limit_bytes,
                "device_limit": sub.device_limit,
                "subscription_url": sub.subscription_url,
                "autopay_enabled": sub.autopay_enabled,
                "panel_username": sub.panel_username,
                "source": (sub.plan_snapshot or {}).get("source") or "bot",
            }
        return detail


class BalanceIn(BaseModel):
    amount_minor: int = Field(..., ge=-10_000_000_00, le=10_000_000_00)
    comment: str = Field("", max_length=256)


@router.post("/{user_id}/balance", response_model=OkOut)
async def change_balance(
    user_id: int,
    body: BalanceIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    if body.amount_minor == 0:
        raise HTTPException(400, "amount must be non-zero")
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if body.amount_minor < 0 and user.balance_minor + body.amount_minor < 0:
            raise HTTPException(400, "insufficient balance")
        await uow.users.increment_balance(user, body.amount_minor)
        now = dt.datetime.now(dt.UTC)
        await uow.transactions.add(
            Transaction(
                user_id=user.id,
                type=TransactionType.GIFT if body.amount_minor > 0 else TransactionType.WITHDRAWAL,
                status=TransactionStatus.COMPLETED,
                amount_minor=abs(body.amount_minor),
                currency=user.currency,
                payment_method="admin",
                gateway_display_name=f"admin @{identity.username}",
                completed_at=now,
            )
        )
        await audit(
            uow,
            identity,
            "user.balance",
            f"user:{user_id}",
            amount_minor=body.amount_minor,
            comment=body.comment,
        )
        await uow.commit()
    return OkOut()


class ExtendIn(BaseModel):
    # Either add N days (relative), set the expiry to an absolute date (calendar; may shorten),
    # or make it open-ended («бессрочно» = now + UNLIMITED_EXPIRE_DAYS, as the panel stores it).
    days: int | None = Field(None, ge=1, le=3650)
    until: dt.date | None = None
    forever: bool = False

    @model_validator(mode="after")
    def _exactly_one(self) -> ExtendIn:
        if (self.days is not None) + (self.until is not None) + self.forever != 1:
            raise ValueError("provide exactly one of days / until / forever")
        if self.until is not None and self.until > _LAST_DAY:
            raise ValueError("дата окончания — не позже 31.12.2099")
        return self


@router.post("/{user_id}/extend", response_model=OkOut)
async def extend_subscription(
    user_id: int,
    body: ExtendIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if not user.current_subscription_id:
            raise HTTPException(400, "user has no subscription")
        sub = await uow.subscriptions.get(user.current_subscription_id)
        if sub is None:
            raise HTTPException(400, "subscription missing")
        await _refuse_service(container, uow, user)
        try:
            if body.until is not None or body.forever:
                # End of the chosen day, UTC — so "until 5 Aug" keeps access through the 5th.
                target = (
                    dt.datetime.combine(body.until, dt.time(23, 59, 59), tzinfo=dt.UTC)
                    if body.until is not None
                    else dt.datetime.now(dt.UTC) + dt.timedelta(days=UNLIMITED_EXPIRE_DAYS)
                )
                await container.subscriptions.set_expiry(
                    uow, sub, expire_at=target, telegram_id=user.telegram_id
                )
            else:
                assert body.days is not None
                await container.subscriptions.renew(
                    uow, sub, days=body.days, telegram_id=user.telegram_id
                )
        except RemnawaveError as exc:
            raise _panel_error(exc) from exc
        except DomainError as exc:
            raise HTTPException(400, str(exc)) from exc
        if body.forever:
            detail = "forever"
        else:
            detail = f"until={body.until}" if body.until else f"days={body.days}"
        await audit(uow, identity, "user.extend", f"user:{user_id}", detail=detail)
        await uow.commit()
    _touched(container)
    return OkOut()


class GrantIn(BaseModel):
    plan_id: int
    days: int = Field(..., ge=1, le=3650)


@router.post("/{user_id}/grant", response_model=OkOut)
async def grant_subscription(
    user_id: int,
    body: GrantIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """Выдать подписку по тарифу (не просто дни) — как подарок, без оплаты.

    ``/extend`` умеет только продлить существующую подписку и отвечает 400, если её нет, —
    поэтому выдать тариф новому пользователю из админки было нельзя. Здесь: нет подписки →
    заводим на панели; есть тот же тариф (или перенесённая без тарифа) → продлеваем ту же
    учётку; другой тариф → смена тарифа на той же учётке. Дубликат на панели не создаётся.
    """
    from src.application.dto.pricing import PurchaseRequest
    from src.core.enums import PurchaseType

    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        plan = await uow.plans.get(body.plan_id)
        if plan is None:
            raise HTTPException(404, "plan not found")
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
        await _refuse_service(container, uow, user)
        req = PurchaseRequest(
            user_id=user.id,
            plan_id=plan.id,
            duration_days=body.days,
            currency=user.currency,
            purchase_type=PurchaseType.NEW,
        )
        try:
            if sub is None or sub.panel_ref is None:
                # mark_paid=False: подарок от админа — не помечаем юзера как платившего
                # (иначе ломается сегментация промо «только новым» и конверсия в отчётах).
                await container.subscriptions.grant(
                    uow, user=user, plan=plan, req=req, is_trial=False, mark_paid=False
                )
                mode = "new"
            elif sub.plan_id is not None and sub.plan_id != plan.id:
                await container.subscriptions.change(uow, sub, user=user, plan=plan, req=req)
                mode = "change"
            else:
                await container.subscriptions.renew(
                    uow,
                    sub,
                    days=body.days,
                    telegram_id=user.telegram_id,
                    adopt_plan=plan if sub.plan_id is None else None,
                )
                mode = "renew"
        except RemnawaveError as exc:
            raise _panel_error(exc) from exc
        except DomainError as exc:
            raise HTTPException(400, str(exc)) from exc
        await audit(
            uow,
            identity,
            "user.grant",
            f"user:{user_id}",
            detail=f"plan={plan.id} days={body.days} mode={mode}",
        )
        await uow.commit()
    _touched(container)
    return OkOut()


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.post("/{user_id}/message", response_model=OkOut)
async def message_user(
    user_id: int,
    body: MessageIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """DM the customer from the bot — operators asked not to leave the cabinet for this."""
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if user.telegram_id is None:
            raise HTTPException(400, "user has no telegram account")
        telegram_id = user.telegram_id
        await audit(uow, identity, "user.message", f"user:{user_id}", chars=len(body.text))
        await uow.commit()
    # The notifier swallows send errors (a blocked user must not break background sweeps), so
    # trust its verdict rather than the absence of an exception — otherwise the admin sees "✓"
    # for a message that bounced.
    if not await container.notifier.notify_user(telegram_id, body.text):
        raise HTTPException(502, "не доставлено: пользователь заблокировал бота или чат не найден")
    return OkOut()


class DiscountIn(BaseModel):
    percent: int = Field(..., ge=0, le=100)


@router.post("/{user_id}/discount", response_model=OkOut)
async def set_discount(
    user_id: int,
    body: DiscountIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """Personal discount for this customer (0 clears it)."""
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        user.personal_discount_pct = body.percent
        await audit(uow, identity, "user.discount", f"user:{user_id}", percent=body.percent)
        await uow.commit()
    return OkOut()


class TrialIn(BaseModel):
    available: bool


@router.post("/{user_id}/trial", response_model=OkOut)
async def set_trial(
    user_id: int,
    body: TrialIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """Give back or take away the free-trial entitlement."""
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        user.is_trial_available = body.available
        await audit(uow, identity, "user.trial", f"user:{user_id}", available=body.available)
        await uow.commit()
    return OkOut()


@router.post("/{user_id}/sync", response_model=OkOut)
async def sync_user(
    user_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """Re-push the subscription to the panel when local data and the panel drifted apart."""
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
        if sub is None or sub.panel_ref is None:
            raise HTTPException(400, "у пользователя нет подписки на панели")  # noqa: RUF001
        await _refuse_service(container, uow, user)
        try:
            await container.subscriptions.push_limits(uow, sub, telegram_id=user.telegram_id)
            if sub.status.is_usable:
                # Limits alone left a hand-disabled panel user DISABLED while the bot said
                # «Активна» (field: user_5072755730). The bot is the source of truth.
                await container.remnawave_client.enable_user(
                    replace(sub.panel_ref, telegram_id=user.telegram_id)
                )
        except RemnawaveError as exc:
            raise _panel_error(exc) from exc
        await audit(uow, identity, "user.sync", f"user:{user_id}")
        await uow.commit()
    _touched(container)
    return OkOut()


@router.post("/{user_id}/block", response_model=OkOut)
async def block_user(
    user_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    return await _set_status(container, identity, user_id, UserStatus.BLOCKED)


@router.post("/{user_id}/unblock", response_model=OkOut)
async def unblock_user(
    user_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    return await _set_status(container, identity, user_id, UserStatus.ACTIVE)


async def _set_status(
    container: AppContainer, identity: AdminIdentity, user_id: int, status: UserStatus
) -> OkOut:
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if user.role.is_staff and status is UserStatus.BLOCKED:
            raise HTTPException(400, "cannot block a staff account")
        user.status = status
        await audit(uow, identity, f"user.{status.value}", f"user:{user_id}")
        await uow.commit()
    return OkOut()


class DeviceLimitIn(BaseModel):
    delta: int = Field(1, ge=-10, le=10)
    # An exact limit typed by the admin (wins over ``delta``) — no clicking +1 twenty times.
    value: int | None = Field(None, ge=1, le=100)


@router.post("/{user_id}/hwid", response_model=OkOut)
async def change_device_limit(
    user_id: int,
    body: DeviceLimitIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None or not user.current_subscription_id:
            raise HTTPException(400, "user has no subscription")
        sub = await uow.subscriptions.get(user.current_subscription_id)
        if sub is None:
            raise HTTPException(400, "subscription missing")
        await _refuse_service(container, uow, user)
        new_limit = (
            body.value if body.value is not None else max(1, (sub.device_limit or 1) + body.delta)
        )
        sub.device_limit = new_limit
        panel_ref = sub.panel_ref
        if panel_ref is not None and user.telegram_id is not None:
            panel_ref = replace(panel_ref, telegram_id=user.telegram_id)
        if panel_ref is not None and sub.expire_at is not None:
            spec = container.remnawave.build_spec(
                short_id=sub.short_id,
                telegram_id=user.telegram_id,
                expire_at=sub.expire_at,
                traffic_limit_bytes=sub.traffic_limit_bytes,
                device_limit=new_limit,
                internal_squads=tuple(sub.internal_squads or ()),
                external_squad=sub.external_squad,
            )
            try:
                await container.remnawave.apply(panel_ref, spec)
            except RemnawaveError as exc:
                raise _panel_error(exc) from exc
        await audit(uow, identity, "user.hwid", f"user:{user_id}", new_limit=new_limit)
        await uow.commit()
    _touched(container)
    return OkOut()


class LimitsIn(BaseModel):
    """Quick edits from the client card: traffic cap, device cap, panel tag."""

    traffic_gb: int | None = Field(None, ge=0, le=1_000_000)  # 0 = unlimited
    device_limit: int | None = Field(None, ge=0, le=100)  # 0 = unlimited
    tag: str | None = Field(None, max_length=16)  # "" clears the tag

    @field_validator("tag")
    @classmethod
    def _tag(cls, value: str | None) -> str | None:
        return _clean_tag(value)

    @model_validator(mode="after")
    def _something(self) -> LimitsIn:
        if self.traffic_gb is None and self.device_limit is None and self.tag is None:
            raise ValueError("nothing to change")
        return self


@router.post("/{user_id}/limits", response_model=OkOut)
async def change_limits(
    user_id: int,
    body: LimitsIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """Panel-first: the panel user gets the new limits/tag, then the bot's row follows."""
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None or not user.current_subscription_id:
            raise HTTPException(400, "user has no subscription")
        sub = await uow.subscriptions.get(user.current_subscription_id)
        if sub is None or sub.panel_ref is None:
            raise HTTPException(400, "subscription is not on the panel")
        await _refuse_service(container, uow, user)
        now = dt.datetime.now(dt.UTC)
        in_grace = sub.grace_until is not None and sub.grace_until > now
        if in_grace and body.traffic_gb is not None:
            raise HTTPException(
                409,
                "подписка в льготном периоде: трафик сейчас задаёт льгота — "
                "сначала продлите подписку, потом меняйте лимит",
            )
        traffic = (
            body.traffic_gb * BYTES_PER_GB
            if body.traffic_gb is not None
            else sub.traffic_limit_bytes
        )
        devices = body.device_limit if body.device_limit is not None else sub.device_limit
        # Only the fields that were passed reach the panel: the expiry and the rest stay as the
        # panel has them (a grace window's short expiry / small cap included).
        only = {
            field
            for field, given in (
                ("trafficLimitBytes", body.traffic_gb is not None),
                ("hwidDeviceLimit", body.device_limit is not None),
                ("tag", body.tag is not None),
            )
            if given
        }
        spec = container.remnawave.build_spec(
            short_id=sub.short_id,
            telegram_id=user.telegram_id,
            expire_at=sub.expire_at or now,  # not sent (see ``only``)
            traffic_limit_bytes=traffic,
            device_limit=devices,
            tag=body.tag or None,
        )
        spec = replace(
            spec,
            only=frozenset(only),
            # "" clears the tag (an omitted one would be kept)
            extra={"tag": None} if body.tag == "" else {},
        )
        try:
            await container.remnawave.apply(
                replace(sub.panel_ref, telegram_id=user.telegram_id), spec
            )
        except RemnawaveError as exc:
            raise _panel_error(exc) from exc
        sub.traffic_limit_bytes = traffic
        sub.device_limit = devices
        await audit(
            uow,
            identity,
            "user.limits",
            f"user:{user_id}",
            traffic_gb=body.traffic_gb,
            device_limit=body.device_limit,
            tag=body.tag,
        )
        await uow.commit()
    _touched(container)
    return OkOut()


@router.post("/{user_id}/reset-traffic", response_model=OkOut)
async def reset_traffic(
    user_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None or not user.current_subscription_id:
            raise HTTPException(400, "user has no subscription")
        sub = await uow.subscriptions.get(user.current_subscription_id)
        if sub is None:
            raise HTTPException(400, "subscription missing")
        await _refuse_service(container, uow, user)
        # Panel-first: reset on Remnawave, else a user.updated webhook re-mirrors the real usage
        # and the local zero is reverted — a traffic-LIMITED user would stay throttled (#3).
        panel_ref = sub.panel_ref
        if panel_ref is not None:
            if user.telegram_id is not None:
                panel_ref = replace(panel_ref, telegram_id=user.telegram_id)
            try:
                await container.remnawave_client.reset_traffic(panel_ref)
            except RemnawaveError as exc:
                raise _panel_error(exc) from exc
        sub.traffic_used_bytes = 0
        await audit(uow, identity, "user.reset_traffic", f"user:{user_id}")
        await uow.commit()
    _touched(container)
    return OkOut()


@router.post("/{user_id}/reset-devices")
async def reset_devices(
    user_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Unbind every registered HWID device — the user can reconnect on the same slots."""
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None or not user.current_subscription_id:
            raise HTTPException(400, "user has no subscription")
        sub = await uow.subscriptions.get(user.current_subscription_id)
        await _refuse_service(container, uow, user)
        panel_ref = sub.panel_ref if sub else None
        if panel_ref is not None and user.telegram_id is not None:
            panel_ref = replace(panel_ref, telegram_id=user.telegram_id)
    if panel_ref is None:
        raise HTTPException(400, "subscription is not on the panel")
    try:
        devices = await container.remnawave_client.get_devices(panel_ref)
        for d in devices:
            await container.remnawave_client.delete_device(panel_ref, d.hwid)
    except RemnawaveError as exc:
        raise _panel_error(exc) from exc
    async with container.uow() as uow:
        await audit(uow, identity, "user.reset_devices", f"user:{user_id}", removed=len(devices))
        await uow.commit()
    _touched(container)
    return {"ok": True, "removed": len(devices)}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    """Hard-delete a user: remove their panel user(s) first, then the local record (FKs cascade
    subscriptions/transactions/tickets/…). Staff accounts are protected."""
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if user.role.is_staff:
            raise HTTPException(400, "cannot delete a staff account")
        await _refuse_service(container, uow, user)
        panel_refs = [
            replace(ref, telegram_id=user.telegram_id) if user.telegram_id is not None else ref
            for s in (await uow.subscriptions.list(user_id=user_id))
            if (ref := s.panel_ref)
        ]
        label = user.username or str(user.telegram_id or user_id)
        # Panel-first, best-effort: a refunded/deleted user must not keep connecting. A panel
        # blip shouldn't block the local delete — log and proceed (rare orphan cleanable by hand).
        for panel_ref in panel_refs:
            with contextlib.suppress(Exception):
                await container.remnawave_client.delete_user(panel_ref)
        await audit(uow, identity, "user.delete", f"user:{label}")
        # Direct DELETE so the DB's ON DELETE CASCADE clears subscriptions/transactions/tickets/…
        # (session.delete would depend on relationship config and could hit a NOT NULL FK).
        await uow.session.execute(sa_delete(User).where(User.id == user_id))
        await uow.commit()
    _touched(container)
    return OkOut()
