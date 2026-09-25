"""Router customer onboarding: trial on the base router plan, QR claim, router plan catalogue.

One subscription per customer: the router rides on the customer's current subscription (the
same panel user the family's phones use), so every bot purchase — which renews/changes the
CURRENT subscription — automatically carries the router along. A brand-new customer is
represented until they scan the QR by a placeholder account with no Telegram (same shape as a
web-cabinet account), which the scan merges into their Telegram account.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import secrets
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from src.application.dto.pricing import PurchaseRequest
from src.application.services.account_link import merge_web_into_telegram
from src.core.enums import AuthType, Currency, PlanCategory, PurchaseType, SubscriptionStatus
from src.core.logging import get_logger
from src.infrastructure.database.models.user import User

if TYPE_CHECKING:
    from src.application.services.remnawave import RemnawaveService
    from src.application.services.subscription import SubscriptionService
    from src.infrastructure.database.models.plan import Plan
    from src.infrastructure.database.models.router_device import RouterDevice
    from src.infrastructure.database.models.subscription import Subscription
    from src.infrastructure.database.uow import UnitOfWork

log = get_logger(__name__)

CLAIM_START_PREFIX = "router_"
PLANS_START_PARAM = "routerplans"
_MSK = dt.timedelta(hours=3)


class RouterOnboardingError(Exception):
    """Message is safe to show to the customer / technician."""


def new_claim_code() -> str:
    # token_urlsafe only emits [A-Za-z0-9_-] — exactly Telegram's allowed start-param alphabet.
    return secrets.token_urlsafe(12)


def claim_url(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={CLAIM_START_PREFIX}{code}"


def plans_url(bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start={PLANS_START_PARAM}"


def fmt_date(value: dt.datetime | None) -> str:
    return (value + _MSK).strftime("%d.%m.%Y") if value else "—"


async def router_plans(uow: UnitOfWork) -> list[Plan]:
    """Active, sellable router-category plans (the family variants), catalogue order."""
    return [
        p
        for p in await uow.plans.list_with_durations()
        if p.is_active and not p.is_trial and p.category is PlanCategory.ROUTER and p.durations
    ]


def cheapest_rub(plan: Plan) -> int | None:
    prices = [
        pr.price_minor for d in plan.durations for pr in d.prices if pr.currency is Currency.RUB
    ]
    return min(prices) if prices else None


def duration_rub(plan: Plan, days: int) -> int | None:
    for d in plan.durations:
        if d.days == days:
            return next((pr.price_minor for pr in d.prices if pr.currency is Currency.RUB), None)
    return None


async def tag_router_subscription(
    remnawave: RemnawaveService, uow: UnitOfWork, sub: Subscription
) -> None:
    """Best-effort: tag the panel user ROUTER (staff visibility in Remnawave's UI) and push the
    owner's telegram id. Never blocks onboarding on panel availability."""
    panel_ref = sub.panel_ref
    if panel_ref is None:
        return
    user = await uow.users.get(sub.user_id)
    telegram_id = user.telegram_id if user else None
    spec = remnawave.build_spec(
        short_id=sub.short_id,
        telegram_id=telegram_id,
        expire_at=sub.expire_at or dt.datetime.now(dt.UTC),
        traffic_limit_bytes=sub.traffic_limit_bytes,
        device_limit=sub.device_limit,
        internal_squads=tuple(sub.internal_squads or ()),
        external_squad=sub.external_squad,
        tag="ROUTER",
    )
    try:
        await remnawave.apply(replace(panel_ref, telegram_id=telegram_id), spec)
    except Exception as exc:
        log.warning("router: panel tag failed", sub_id=sub.id, error=str(exc))


async def start_trial(
    uow: UnitOfWork, subscriptions: SubscriptionService, *, user: User, days: int
) -> Subscription:
    """A router trial on the base (first) router plan. It does NOT spend the person's own app
    trial — the technician grants this one, the customer never asked for it."""
    plans = await router_plans(uow)
    if not plans:
        raise RouterOnboardingError(
            "нет активного тарифа с категорией «Роутер» — создайте его в «Тарифах»"
        )
    plan = plans[0]
    had_trial = user.is_trial_available
    req = PurchaseRequest(
        user_id=user.id,
        plan_id=plan.id,
        duration_days=max(days, 1),
        currency=user.currency,
        purchase_type=PurchaseType.NEW,
    )
    sub = await subscriptions.grant(uow, user=user, plan=plan, req=req, is_trial=True)
    user.is_trial_available = had_trial
    return sub


async def new_placeholder_customer(uow: UnitOfWork, *, label: str) -> User:
    from src.application.services.ids import generate_referral_code

    user = User(
        first_name=f"Роутер «{label}» (ждёт QR)"[:128],
        auth_type=AuthType.EMAIL,
        referral_code=generate_referral_code(),
    )
    await uow.users.add(user)
    return user


async def claim(
    uow: UnitOfWork,
    *,
    subscriptions: SubscriptionService,
    remnawave: RemnawaveService,
    user: User,
    code: str,
) -> RouterDevice:
    """Bind the router behind ``code`` to ``user`` (the Telegram account that scanned the QR).

    The placeholder account (and its trial) merges into the customer's account. If the customer
    already has their own live subscription, the router joins THAT one and the router trial is
    retired — one subscription per customer, so their purchases always extend the router too.
    """
    device = await uow.router_devices.find_one(claim_code=code)
    if device is None:
        raise RouterOnboardingError("ссылка уже использована или устарела")
    sub = await uow.subscriptions.get(device.subscription_id)
    owner = await uow.users.get(sub.user_id) if sub is not None else None
    if owner is None:
        raise RouterOnboardingError("роутер не найден — попросите мастера создать его заново")
    if owner.id != user.id:
        if owner.telegram_id is not None:
            raise RouterOnboardingError("этот роутер уже привязан к другому аккаунту")
        await merge_web_into_telegram(uow, user, owner.id)

    trial_id = device.subscription_id
    current_id = user.current_subscription_id
    if current_id and current_id != trial_id:
        current = await uow.subscriptions.get(current_id)
        if current is not None and current.status.is_usable:
            await _retire_trial(uow, subscriptions, trial_id, user)
            for other in await uow.router_devices.list(subscription_id=trial_id):
                other.subscription_id = current_id
    device.claim_code = None
    await uow.flush()
    live = await uow.subscriptions.get(device.subscription_id)
    if live is not None:
        await tag_router_subscription(remnawave, uow, live)
    return device


async def _retire_trial(
    uow: UnitOfWork, subscriptions: SubscriptionService, trial_id: int, user: User
) -> None:
    trial = await uow.subscriptions.get(trial_id)
    if trial is None or not trial.is_trial:
        return
    with contextlib.suppress(Exception):
        await subscriptions.set_expiry(
            uow, trial, expire_at=dt.datetime.now(dt.UTC), telegram_id=user.telegram_id
        )
    trial.status = SubscriptionStatus.EXPIRED


def status_line(sub: Any) -> str:
    if sub is None or not sub.status.is_usable:
        return "Подписка не активна — VPN на роутере выключен до оплаты."
    if sub.is_trial:
        return f"Пробный период до {fmt_date(sub.expire_at)}."
    return f"Подписка активна до {fmt_date(sub.expire_at)}."


CASH_DISPLAY_NAME = "Наличные (мастер)"


def attached_text(label: str, sub: Any, bot_username: str) -> str:
    """DM to an existing bot user a technician picked by @username (they've pressed Start
    before, so the bot may message them)."""
    lines = [f"📡 К вашему аккаунту подключён роутер «{label}».", status_line(sub)]
    if bot_username:
        lines.append(f"Выбрать тариф и оплатить: {plans_url(bot_username)}")
    return "\n\n".join(lines)


def paid_text(plan_name: str, sub: Any) -> str:
    return (
        f"✅ Оплата получена: тариф «{plan_name}».\n"
        f"Подписка активна до {fmt_date(sub.expire_at if sub else None)}. Спасибо!"
    )
