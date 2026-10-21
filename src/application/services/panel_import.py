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


def _url_code(url: str | None) -> str | None:
    """The panel's short uuid at the end of a subscription link (…/api/sub/<code>)."""
    if not url:
        return None
    code = url.rstrip("/").rsplit("/", 1)[-1]
    return code or None


def _panel_keys(p: PanelUser) -> set[str]:
    keys = set()
    if p.panel_id is not None:
        keys.add(f"id:{p.panel_id}")
    if p.uuid is not None:
        keys.add(f"uuid:{p.uuid}")
    if p.short_id:
        keys.add(f"code:{p.short_id}")
    code = _url_code(p.subscription_url)
    if code:
        keys.add(f"code:{code}")
    return keys


def _sub_keys(sub: Subscription) -> set[str]:
    """Every way a bot subscription can name its panel user. The link code matters: a
    subscription from the 2.x era knows its panel user by uuid, which 3.x payloads no longer
    carry — matched by uuid alone it looked «panel-only» and got adopted a second time."""
    keys = set()
    if sub.remnawave_id is not None:
        keys.add(f"id:{sub.remnawave_id}")
    if sub.remnawave_uuid is not None:
        keys.add(f"uuid:{sub.remnawave_uuid}")
    if sub.short_id:
        keys.add(f"code:{sub.short_id}")
    code = _url_code(sub.subscription_url)
    if code:
        keys.add(f"code:{code}")
    return keys


async def find_panel_only(uow: UnitOfWork, panel_users: list[PanelUser]) -> list[PanelOnlyUser]:
    """Panel users no bot subscription points at (panel id, uuid, short uuid or link code)."""
    subs = await uow.subscriptions.list()
    linked: set[str] = set()
    for sub in subs:
        linked |= _sub_keys(sub)
    out: list[PanelOnlyUser] = []
    for p in panel_users:
        if _panel_keys(p) & linked:
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


# --- duplicates: two bot subscriptions on one panel user ---------------------------------


@dataclass(frozen=True, slots=True)
class Duplicate:
    panel: PanelUser
    keep: Subscription
    extra: list[Subscription]

    def as_dict(self) -> dict[str, Any]:
        return {
            "panel_username": self.panel.username,
            "panel_id": self.panel.panel_id,
            "user_id": self.keep.user_id,
            "keep": {"id": self.keep.id, "status": self.keep.status.value},
            "extra": [
                {
                    "id": x.id,
                    "status": x.status.value,
                    "from_import": (x.plan_snapshot or {}).get("source") == "panel",
                }
                for x in self.extra
            ],
        }


async def find_duplicates(uow: UnitOfWork, panel_users: list[PanelUser]) -> list[Duplicate]:
    """Panel users more than one bot subscription points at. Dangerous, not cosmetic: a
    non-working duplicate makes the 7-minute reconcile job disable the panel user again and
    again, cutting off the person whose real subscription is paid."""
    subs = list(await uow.subscriptions.list())
    out: list[Duplicate] = []
    for p in panel_users:
        keys = _panel_keys(p)
        group = [s for s in subs if _sub_keys(s) & keys]
        if len(group) < 2:
            continue
        currents = set()
        for s in group:
            owner = await uow.users.get(s.user_id)
            if owner is not None and owner.current_subscription_id == s.id:
                currents.add(s.id)

        # keep: a working one first; then the ORIGINAL over the import's copy (field: 15
        # pairs, both expired — the import's copy had become «current» and was kept, while the
        # original bought in the bot was marked extra and, rightly, never deleted -> repair did
        # nothing); then the current one; then the newest.
        def from_import(s: Subscription) -> bool:
            return (s.plan_snapshot or {}).get("source") == "panel"

        group.sort(
            key=lambda s: (s.status.is_usable, not from_import(s), s.id in currents, s.id),
            reverse=True,
        )
        out.append(Duplicate(p, group[0], group[1:]))
    return out


async def repair_duplicates(uow: UnitOfWork, dupes: list[Duplicate]) -> dict[str, int]:
    """Remove the extra subscriptions this importer created; give the kept one the panel's
    number so it is recognised directly from now on. A duplicate the importer did NOT create
    (bought in the bot) is left alone and only reported. Caller commits."""
    removed = skipped = 0
    for d in dupes:
        if d.keep.remnawave_id is None and d.panel.panel_id is not None:
            d.keep.remnawave_id = d.panel.panel_id
        for extra in d.extra:
            if (extra.plan_snapshot or {}).get("source") != "panel":
                skipped += 1
                continue
            owner = await uow.users.get(extra.user_id)
            if owner is not None and owner.current_subscription_id == extra.id:
                owner.current_subscription_id = d.keep.id if d.keep.user_id == owner.id else None
                # written before the delete: users.current_subscription_id references it
                await uow.session.flush()
            await uow.session.delete(extra)
            removed += 1
    await uow.session.flush()
    return {"removed": removed, "skipped": skipped}
