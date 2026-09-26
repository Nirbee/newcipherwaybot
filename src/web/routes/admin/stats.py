"""Admin: product statistics — user & subscription breakdowns with a 14-day chart (Продукт)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from src.core.enums import (
    PurchaseType,
    SubscriptionStatus,
    TransactionStatus,
    TransactionType,
    UserStatus,
)
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import day_bounds_utc

router = APIRouter(prefix="/stats")

_LIVE = (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.LIMITED)


async def _count(uow: Any, stmt: Any) -> int:
    return int(await uow.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


async def _users_created_since(uow: Any, since: dt.datetime) -> int:
    return await _count(uow, select(User.id).where(User.created_at >= since))


async def _subs_in(uow: Any, *statuses: SubscriptionStatus) -> int:
    return await _count(uow, select(Subscription.id).where(Subscription.status.in_(statuses)))


_PERIODS = (7, 30, 90)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _series(values: list[dt.datetime | None], start: dt.datetime, days: int) -> list[int]:
    out = [0] * days
    for v in values:
        if v is None:
            continue
        idx = (_aware(v) - start).days
        if 0 <= idx < days:
            out[idx] += 1
    return out


@router.get("")
async def stats(days: int = 30, container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    days = days if days in _PERIODS else 30
    now = dt.datetime.now(dt.UTC)
    today_start, _ = day_bounds_utc(0)
    start = today_start - dt.timedelta(days=days - 1)
    dates = [(start + dt.timedelta(days=i)).date().isoformat() for i in range(days)]
    async with container.uow() as uow:
        total = await uow.users.count()

        # Users linked to a live subscription (their current one).
        with_sub_stmt = (
            select(User.id)
            .join(Subscription, Subscription.id == User.current_subscription_id)
            .where(Subscription.status.in_(_LIVE))
        )
        with_sub = await _count(uow, with_sub_stmt)
        with_trial = await _count(
            uow,
            select(User.id)
            .join(Subscription, Subscription.id == User.current_subscription_id)
            .where(Subscription.status == SubscriptionStatus.TRIAL),
        )

        created = [
            r[0]
            for r in (
                await uow.session.execute(select(User.created_at).where(User.created_at >= start))
            ).all()
        ]
        users = {
            "total": total,
            "new_today": await _users_created_since(uow, today_start),
            "new_week": await _users_created_since(uow, now - dt.timedelta(days=7)),
            "new_month": await _users_created_since(uow, now - dt.timedelta(days=30)),
            "new_period": len(created),
            "with_sub": with_sub,
            "without_sub": max(0, total - with_sub),
            "with_trial": with_trial,
            "blocked": await _count(uow, select(User.id).where(User.status == UserStatus.BLOCKED)),
            "bot_blocked": await _count(
                uow, select(User.id).where(User.bot_blocked_at.is_not(None))
            ),
            "chart": [
                {"date": d, "count": c}
                for d, c in zip(dates, _series(created, start, days), strict=True)
            ],
        }

        # Subscription purchases (any funding source) in the period — the sales rhythm.
        purchases = (
            await uow.session.execute(
                select(Transaction.completed_at, Transaction.purchase_type)
                .where(Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT)
                .where(Transaction.status == TransactionStatus.COMPLETED)
                .where(Transaction.completed_at >= start)
                .where(Transaction.is_test.is_(False))
            )
        ).all()
        purchases = [p for p in purchases if p[1] is not PurchaseType.TRAFFIC_TOPUP]
        kinds = {"new": 0, "renew": 0, "change": 0}
        for _at, ptype in purchases:
            if ptype is PurchaseType.RENEW:
                kinds["renew"] += 1
            elif ptype is PurchaseType.CHANGE:
                kinds["change"] += 1
            else:
                kinds["new"] += 1
        sales = {
            "chart": [
                {"date": d, "count": c}
                for d, c in zip(dates, _series([p[0] for p in purchases], start, days), strict=True)
            ],
            "total": len(purchases),
            "kinds": kinds,
        }

        live_traffic = (
            select(Subscription.id, Subscription.traffic_limit_bytes)
            .where(Subscription.status.in_(_LIVE))
            .subquery()
        )
        unlimited = int(
            await uow.session.scalar(
                select(func.count()).where(
                    (live_traffic.c.traffic_limit_bytes == 0)
                    | live_traffic.c.traffic_limit_bytes.is_(None)
                )
            )
            or 0
        )
        limited = int(
            await uow.session.scalar(
                select(func.count()).where(live_traffic.c.traffic_limit_bytes > 0)
            )
            or 0
        )
        # Live subscriptions per plan — what customers actually run.
        per_plan = (
            await uow.session.execute(
                select(Subscription.plan_id, func.count())
                .where(Subscription.status.in_(_LIVE), Subscription.is_trial.is_(False))
                .group_by(Subscription.plan_id)
            )
        ).all()
        plans = {p.id: p for p in await uow.plans.list()}
        top_plans = sorted(
            (
                {
                    "name": plans[pid].name if pid in plans else "Без тарифа",
                    "category": plans[pid].category.value if pid in plans else "app",
                    "count": int(cnt),
                }
                for pid, cnt in per_plan
            ),
            key=lambda x: -x["count"],
        )
        subs = {
            "active": await _subs_in(uow, SubscriptionStatus.ACTIVE, SubscriptionStatus.LIMITED),
            "disabled": await _subs_in(uow, SubscriptionStatus.DISABLED),
            "expired": await _subs_in(uow, SubscriptionStatus.EXPIRED),
            "trial": await _subs_in(uow, SubscriptionStatus.TRIAL),
            "unlimited_traffic": unlimited,
            "limited_traffic": limited,
            "expired_period": await _count(
                uow,
                select(Subscription.id).where(
                    Subscription.status == SubscriptionStatus.EXPIRED,
                    Subscription.expire_at >= start,
                ),
            ),
            "top_plans": top_plans[:8],
        }

    return {"days": days, "users": users, "subs": subs, "sales": sales}
