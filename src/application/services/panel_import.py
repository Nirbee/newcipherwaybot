"""Adopt panel-only users into the bot («Синхронизация с панелью»).

Some people exist only in Remnawave — created by hand, or by an earlier bot — so the bot
neither lists them nor manages their subscription. This finds them and creates the bot side
for the ones the admin picks: a user (matched to an existing bot user by Telegram ID when
there is one) and a subscription that points at the SAME panel user. Nothing is created or
changed in the panel — the person's links and devices keep working.

The panel username (``user_5072755730``) can't be renamed — Remnawave addresses users by it —
so the bot shows people the way it shows everyone else: by Telegram name and ID.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.application.services.ids import generate_referral_code, generate_short_id
from src.core.enums import Currency, SubscriptionStatus
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

if TYPE_CHECKING:
    from src.application.dto.panel import PanelUser
    from src.infrastructure.database.uow import UnitOfWork

_TG_IN_TEXT = re.compile(r"(?<!\d)(\d{6,12})(?!\d)")
_SHORT_ID_MAX = 16  # Subscription.short_id column width


def telegram_id_of(user: PanelUser) -> int | None:
    """The panel's own telegramId, else one recognisable in the username (``user_5072755730``)
    or the description (``tg:5072755730``) — how earlier bots stored it."""
    if user.telegram_id:
        return int(user.telegram_id)
    for text in (user.username, user.description or ""):
        match = _TG_IN_TEXT.search(text or "")
        if match:
            return int(match.group(1))
    return None


def _key(user: PanelUser) -> str:
    return str(user.panel_id if user.panel_id is not None else user.uuid)


@dataclass(frozen=True, slots=True)
class PanelOnlyUser:
    panel: PanelUser
    telegram_id: int | None
    bot_user_id: int | None  # an existing bot user with that Telegram ID (no subscription link)
    bot_has_live_sub: bool = False  # ...who already has a working subscription of their own

    def as_dict(self) -> dict[str, Any]:
        p = self.panel
        return {
            "key": _key(p),
            "username": p.username,
            "telegram_id": self.telegram_id,
            "bot_user_id": self.bot_user_id,
            "bot_has_live_sub": self.bot_has_live_sub,
            "status": p.status,
            "expire_at": p.expire_at.isoformat() if p.expire_at else None,
            "traffic_used_bytes": p.traffic_used_bytes,
            "traffic_limit_bytes": p.traffic_limit_bytes,
            "device_limit": p.device_limit,
            "tag": p.tag,
            "description": p.description,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }


async def find_panel_only(uow: UnitOfWork, panel_users: list[PanelUser]) -> list[PanelOnlyUser]:
    """Panel users no bot subscription points at (by panel id, uuid or short uuid)."""
    subs = await uow.subscriptions.list()
    linked_ids = {s.remnawave_id for s in subs if s.remnawave_id is not None}
    linked_uuids = {s.remnawave_uuid for s in subs if s.remnawave_uuid is not None}
    linked_shorts = {s.short_id for s in subs}
    out: list[PanelOnlyUser] = []
    for p in panel_users:
        if (
            (p.panel_id is not None and p.panel_id in linked_ids)
            or (p.uuid is not None and p.uuid in linked_uuids)
            or (p.short_id and p.short_id in linked_shorts)
        ):
            continue
        tid = telegram_id_of(p)
        bot_user = await uow.users.find_one(telegram_id=tid) if tid else None
        live = False
        if bot_user is not None and bot_user.current_subscription_id:
            current = await uow.subscriptions.get(bot_user.current_subscription_id)
            live = current is not None and current.status.is_usable
        out.append(PanelOnlyUser(p, tid, bot_user.id if bot_user else None, live))
    out.sort(key=lambda u: (u.telegram_id is None, -(u.panel.panel_id or 0)))
    return out


def _status(p: PanelUser, now: dt.datetime) -> SubscriptionStatus:
    raw = (p.status or "").upper()
    if raw == "DISABLED":
        return SubscriptionStatus.DISABLED
    if raw == "LIMITED":
        return SubscriptionStatus.LIMITED
    expire = p.expire_at
    if raw == "EXPIRED" or (expire is not None and expire <= now):
        return SubscriptionStatus.EXPIRED
    return SubscriptionStatus.ACTIVE


async def adopt(uow: UnitOfWork, candidates: list[PanelOnlyUser], keys: set[str]) -> dict[str, Any]:
    """Create the bot side for the picked panel users. Caller commits."""
    now = dt.datetime.now(dt.UTC)
    summary: dict[str, Any] = {
        "users_created": 0,
        "users_matched": 0,
        "subscriptions": 0,
        "new_user_ids": [],
    }
    for cand in candidates:
        p = cand.panel
        if _key(p) not in keys:
            continue
        user = await uow.users.get(cand.bot_user_id) if cand.bot_user_id else None
        if user is None:
            user = User(
                telegram_id=cand.telegram_id,
                # No Telegram account to name them by -> the panel's name, so they're findable.
                first_name=None if cand.telegram_id else (p.username or None),
                referral_code=generate_referral_code(),
                currency=Currency.RUB,
            )
            await uow.users.add(user)
            await uow.session.flush()  # the subscription below needs user.id
            if p.created_at is not None:
                user.created_at = p.created_at
            summary["users_created"] += 1
            summary["new_user_ids"].append(user)
        else:
            summary["users_matched"] += 1

        short = p.short_id if 0 < len(p.short_id) <= _SHORT_ID_MAX else ""
        if not short or await uow.subscriptions.find_one(short_id=short) is not None:
            short = generate_short_id()
        status = _status(p, now)
        sub = Subscription(
            user_id=user.id,
            remnawave_uuid=p.uuid,
            remnawave_id=p.panel_id,
            short_id=short,
            status=status,
            is_trial=False,
            expire_at=p.expire_at,
            subscription_url=(p.subscription_url or "")[:512] or None,
            traffic_limit_bytes=p.traffic_limit_bytes,
            traffic_used_bytes=p.traffic_used_bytes,
            device_limit=p.device_limit,
            internal_squads=list(p.internal_squads),
            external_squad=p.external_squad,
            plan_snapshot={"name": "Из панели", "source": "panel"},
        )
        await uow.subscriptions.add(sub)
        await uow.session.flush()
        summary["subscriptions"] += 1
        # Never take over a working subscription the person already has in the bot.
        current = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
        if current is None or (status.is_usable and not current.status.is_usable):
            user.current_subscription_id = sub.id
    await uow.session.flush()
    summary["new_user_ids"] = [u.id for u in summary["new_user_ids"]]
    return summary
