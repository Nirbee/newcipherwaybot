"""Admin: dashboard KPIs, revenue chart, system status, event feed (screen 01)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable
from typing import Any, cast

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select

from src.core.enums import (
    RouterDeviceStatus,
    SubscriptionStatus,
    TicketStatus,
    TransactionStatus,
    TransactionType,
)
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.ticket import Ticket
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import day_bounds_utc, iso

router = APIRouter(prefix="/dashboard")

_ROUTER_STALE = dt.timedelta(minutes=15)

# Revenue = external money only. A balance purchase (gateway_type NULL) is an internal
# transfer — its rubles already counted as revenue when the deposit came in; summing both
# double-counts every balance-funded subscription.
_EXTERNAL_MONEY = or_(
    Transaction.type == TransactionType.DEPOSIT,
    (Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT)
    & Transaction.gateway_type.is_not(None),
)


async def _revenue_between(uow: Any, start: dt.datetime, end: dt.datetime) -> int:
    stmt = (
        select(func.coalesce(func.sum(Transaction.amount_minor), 0))
        .where(Transaction.status == TransactionStatus.COMPLETED)
        .where(_EXTERNAL_MONEY)
        .where(Transaction.completed_at >= start, Transaction.completed_at < end)
        .where(Transaction.is_test.is_(False))
    )
    return int(await uow.session.scalar(stmt) or 0)


_PERIODS = (7, 30, 90)


def _period(days: int) -> int:
    return days if days in _PERIODS else 30


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _bucket(at: dt.datetime, start: dt.datetime, days: int) -> int | None:
    idx = (_aware(at) - start).days
    return idx if 0 <= idx < days else None


async def _period_money(
    uow: Any, cur_start: dt.datetime, prev_start: dt.datetime, end: dt.datetime, days: int
) -> dict[str, Any]:
    """One pass over the external money of the current + previous period: the revenue series,
    the delta, the average ticket, paying customers, and the split by product / payment method."""
    rows = (
        await uow.session.execute(
            select(
                Transaction.amount_minor,
                Transaction.completed_at,
                Transaction.type,
                Transaction.gateway_type,
                Transaction.gateway_display_name,
                Transaction.plan_snapshot,
                Transaction.user_id,
            )
            .where(Transaction.status == TransactionStatus.COMPLETED)
            .where(_EXTERNAL_MONEY)
            .where(Transaction.completed_at >= prev_start, Transaction.completed_at < end)
            .where(Transaction.is_test.is_(False))
        )
    ).all()
    categories = {p.id: p.category.value for p in await uow.plans.list()}

    series = [0] * days
    current = previous = orders = 0
    payers: set[int] = set()
    by_product: dict[str, int] = {"app": 0, "router": 0, "premium": 0, "topup": 0}
    by_method: dict[str, list[int]] = {}
    for amount, at, ttype, gateway, gateway_name, snapshot, user_id in rows:
        if at is None:
            continue
        idx = _bucket(at, cur_start, days)
        if idx is None:
            previous += amount
            continue
        series[idx] += amount
        current += amount
        orders += 1
        payers.add(user_id)
        if ttype is TransactionType.DEPOSIT:
            product = "topup"
        else:
            plan_id = (snapshot or {}).get("plan_id")
            product = categories.get(int(plan_id), "app") if str(plan_id).isdigit() else "app"
        by_product[product] = by_product.get(product, 0) + amount
        method = gateway_name or (gateway.value if gateway else "balance")
        slot = by_method.setdefault(method, [0, 0])
        slot[0] += amount
        slot[1] += 1

    return {
        "series": [
            {"date": (cur_start + dt.timedelta(days=i)).date().isoformat(), "amount_minor": v}
            for i, v in enumerate(series)
        ],
        "current_minor": current,
        "previous_minor": previous,
        "orders": orders,
        "payers": len(payers),
        "avg_check_minor": current // orders if orders else 0,
        "by_product": by_product,
        "by_method": sorted(
            (
                {"name": name, "amount_minor": v[0], "count": v[1]}
                for name, v in by_method.items()
            ),
            key=lambda x: -x["amount_minor"],
        ),
    }


async def _new_users_series(uow: Any, start: dt.datetime, days: int) -> list[int]:
    series = [0] * days
    for (created,) in (
        await uow.session.execute(select(User.created_at).where(User.created_at >= start))
    ).all():
        idx = _bucket(created, start, days)
        if idx is not None:
            series[idx] += 1
    return series


async def _fleet(uow: Any, now: dt.datetime) -> dict[str, int]:
    out = {"total": 0, "online": 0, "offline": 0, "pending": 0}
    for d in await uow.router_devices.list(limit=5000):
        if d.status is RouterDeviceStatus.REVOKED:
            continue
        out["total"] += 1
        if d.status is RouterDeviceStatus.PENDING:
            out["pending"] += 1
        elif (
            d.status is RouterDeviceStatus.ONLINE
            and d.last_seen_at is not None
            and now - _aware(d.last_seen_at) < _ROUTER_STALE
        ):
            out["online"] += 1
        else:
            out["offline"] += 1
    return out


async def _count(uow: Any, stmt: Any) -> int:
    return int(await uow.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


@router.get("")
async def dashboard(
    days: int = 30, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    days = _period(days)
    now = dt.datetime.now(dt.UTC)
    today_start, today_end = day_bounds_utc(0)
    cur_start = today_start - dt.timedelta(days=days - 1)
    prev_start = cur_start - dt.timedelta(days=days)
    async with container.uow() as uow:
        yest_start, yest_end = day_bounds_utc(1)
        revenue_today = await _revenue_between(uow, today_start, today_end)
        revenue_yesterday = await _revenue_between(uow, yest_start, yest_end)
        money_stats = await _period_money(uow, cur_start, prev_start, today_end, days)

        live = (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.LIMITED)
        active_subs = await _count(
            uow, select(Subscription.id).where(Subscription.status.in_(live))
        )
        paid_active = await _count(
            uow,
            select(Subscription.id).where(
                Subscription.status.in_(live), Subscription.is_trial.is_(False)
            ),
        )
        expiring_7d = await _count(
            uow,
            select(Subscription.id).where(
                Subscription.status.in_(live),
                Subscription.is_trial.is_(False),
                Subscription.expire_at >= now,
                Subscription.expire_at < now + dt.timedelta(days=7),
            ),
        )
        expired_period = await _count(
            uow,
            select(Subscription.id).where(
                Subscription.status == SubscriptionStatus.EXPIRED,
                Subscription.expire_at >= cur_start,
            ),
        )
        total_users = await uow.users.count()
        trial_used = await _count(uow, select(User.id).where(User.is_trial_available.is_(False)))
        trial_converted = await _count(
            uow,
            select(User.id).where(
                User.is_trial_available.is_(False), User.has_had_paid_subscription.is_(True)
            ),
        )
        new_24h = await _count(
            uow, select(User.id).where(User.created_at >= now - dt.timedelta(hours=24))
        )
        new_trials_24h = await _count(
            uow,
            select(Subscription.id).where(
                Subscription.is_trial.is_(True),
                Subscription.created_at >= now - dt.timedelta(hours=24),
            ),
        )
        users_series = await _new_users_series(uow, cur_start, days)
        prev_new_users = await _count(
            uow,
            select(User.id).where(User.created_at >= prev_start, User.created_at < cur_start),
        )

        nodes = await uow.server_nodes.list()
        online = sum(n.users_online for n in nodes)
        fleet = await _fleet(uow, now)
        tickets_open = await _count(
            uow, select(Ticket.id).where(Ticket.status != TicketStatus.CLOSED)
        )
        tickets_premium = await _count(
            uow,
            select(Ticket.id).where(
                Ticket.status != TicketStatus.CLOSED, Ticket.is_premium.is_(True)
            ),
        )

        events = [
            {
                "id": e.id,
                "at": iso(e.created_at),
                "actor": e.actor_label,
                "action": e.action,
                "entity": e.entity,
            }
            for e in await uow.audit.recent(20)
        ]

        recent_users = select(User).where(User.created_at >= cur_start).subquery()
        src_total = int(
            await uow.session.scalar(select(func.count()).select_from(recent_users)) or 0
        )
        src_campaign = int(
            await uow.session.scalar(
                select(func.count())
                .select_from(recent_users)
                .where(recent_users.c.campaign_id.is_not(None))
            )
            or 0
        )
        src_referral = int(
            await uow.session.scalar(
                select(func.count())
                .select_from(recent_users)
                .where(recent_users.c.referred_by_id.is_not(None))
            )
            or 0
        )

    return {
        "days": days,
        "revenue_today_minor": revenue_today,
        "revenue_yesterday_minor": revenue_yesterday,
        "revenue": money_stats,
        "active_subscriptions": active_subs,
        "paid_active_subscriptions": paid_active,
        "expiring_7d": expiring_7d,
        "expired_period": expired_period,
        "total_users": total_users,
        "new_users_24h": new_24h,
        "new_trials_24h": new_trials_24h,
        "new_users": {
            "series": users_series,
            "current": sum(users_series),
            "previous": prev_new_users,
        },
        "trial_conversion": {
            "used": trial_used,
            "converted": trial_converted,
            "pct": round(100 * trial_converted / trial_used, 1) if trial_used else 0.0,
        },
        "online_now": online,
        "routers": fleet,
        "tickets": {"open": tickets_open, "premium_open": tickets_premium},
        "events": events,
        "sources": {
            "total": src_total,
            "campaigns": src_campaign,
            "referrals": src_referral,
            "organic": max(0, src_total - src_campaign - src_referral),
        },
    }


@router.get("/system")
async def system_status(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Panel/API/DB/Redis health for the «Система» panel."""
    out: dict[str, Any] = {"api": "ok"}
    try:
        # redis-py 7 types ping() as Awaitable[bool] | bool — cast keeps mypy strict happy
        pong = await cast("Awaitable[bool]", container.redis.ping())
        out["redis"] = "ok" if pong else "error"
    except Exception:
        out["redis"] = "error"
    async with container.uow() as uow:
        try:
            size = await uow.session.scalar(select(func.pg_database_size(func.current_database())))
            out["db_size_bytes"] = int(size or 0)
        except Exception:
            await uow.rollback()
            out["db_size_bytes"] = None
        cfg = container.bot_config
        out["maintenance_mode"] = bool(await cfg.value(uow, "MAINTENANCE_MODE"))
        out["backup_enabled"] = bool(await cfg.value(uow, "BACKUP_ENABLED"))
        out["backup_time"] = await cfg.value(uow, "BACKUP_TIME")
    try:
        version = await container.remnawave.ensure_supported()
        out["panel"] = {"status": "ok", "version": version.raw}
    except Exception as exc:
        out["panel"] = {"status": "error", "detail": str(exc)[:200]}
    return out
