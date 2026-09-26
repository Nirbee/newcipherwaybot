"""Premium personal servers: request -> conversation -> personal invoice -> payment.

A customer asks for a server in a given country / for a given purpose; that becomes a premium
support ticket (human-only, top of the admin queue). Once the admin has the server up in
Remnawave (its own squad), they issue an invoice from the ticket: a PREMIUM-category plan
visible and purchasable only by that customer's Telegram id. Paying it goes through the normal
purchase pipeline — it switches the customer's (single) subscription onto the personal squad,
and renewals, reminders and autopay then work like any other plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from src.core.enums import Availability, Currency, PlanCategory, TicketAuthor, TicketStatus
from src.infrastructure.database.base import utcnow
from src.infrastructure.database.models.plan import Plan, PlanDuration, PlanPrice
from src.infrastructure.database.models.ticket import Ticket, TicketMessage

if TYPE_CHECKING:
    from src.infrastructure.database.models.user import User
    from src.infrastructure.database.uow import UnitOfWork

PLAN_START_PREFIX = "plan_"
GIB = 1024**3


def is_premium_plan(plan: Plan | None) -> bool:
    return plan is not None and plan.category is PlanCategory.PREMIUM


def can_buy(plan: Plan, user: User | None) -> bool:
    """A premium offer belongs to the customer it was issued to — nobody else can buy it."""
    if not is_premium_plan(plan):
        return True
    if user is None or user.telegram_id is None:
        return False
    return user.telegram_id in {int(x) for x in (plan.allowed_telegram_ids or [])}


def plan_url(bot_username: str, plan_id: int) -> str:
    return f"https://t.me/{bot_username}?start={PLAN_START_PREFIX}{plan_id}"


async def user_on_premium_plan(uow: UnitOfWork, user: User) -> bool:
    if not user.current_subscription_id:
        return False
    sub = await uow.subscriptions.get(user.current_subscription_id)
    if sub is None or not sub.status.is_usable or sub.plan_id is None:
        return False
    return is_premium_plan(await uow.plans.get(sub.plan_id))


async def active_ticket(uow: UnitOfWork, user_id: int, *, premium_only: bool) -> Ticket | None:
    tickets = await uow.tickets.list(user_id=user_id)
    for t in tickets:
        if t.status is not TicketStatus.CLOSED and (t.is_premium or not premium_only):
            return t
    return None


async def open_request(uow: UnitOfWork, user: User, text: str) -> tuple[Ticket, bool]:
    """File the request as a premium ticket (or append to the open one). Returns (ticket,
    created). An already-open ordinary ticket is upgraded to premium rather than duplicated —
    a user has at most one open ticket."""
    ticket = await active_ticket(uow, user.id, premium_only=False)
    created = ticket is None
    if ticket is None:
        ticket = Ticket(user_id=user.id, subject=f"💎 Премиум-сервер: {text}"[:64], is_premium=True)
        await uow.tickets.add(ticket)
    else:
        ticket.is_premium = True
    await uow.ticket_messages.add(
        TicketMessage(ticket_id=ticket.id, author=TicketAuthor.USER, text=text[:4096])
    )
    ticket.status = TicketStatus.OPEN
    ticket.updated_at = utcnow()
    return ticket, created


async def create_offer(
    uow: UnitOfWork,
    *,
    customer: User,
    name: str,
    durations: Sequence[tuple[int, int]],
    internal_squads: Sequence[str],
    device_limit: int | None,
    traffic_limit_gb: int,
    description: str | None,
) -> Plan:
    """A private PREMIUM plan for exactly this customer (by Telegram id)."""
    if customer.telegram_id is None:
        raise ValueError("customer has no Telegram account")
    code = f"premium-{customer.id}-{utcnow().strftime('%Y%m%d%H%M%S')}"
    plan = Plan(
        public_code=code,
        name=name[:128],
        description=(description or None),
        category=PlanCategory.PREMIUM,
        availability=Availability.ALLOWED,
        allowed_telegram_ids=[customer.telegram_id],
        internal_squads=list(internal_squads),
        device_limit=device_limit,
        traffic_limit_bytes=traffic_limit_gb * GIB if traffic_limit_gb else None,
        is_active=True,
        order_index=10_000,
    )
    await uow.plans.add(plan)
    for i, (days, price_minor) in enumerate(durations):
        duration = PlanDuration(plan_id=plan.id, days=days, order_index=i)
        uow.session.add(duration)
        await uow.flush()
        uow.session.add(
            PlanPrice(plan_duration_id=duration.id, currency=Currency.RUB, price_minor=price_minor)
        )
    await uow.flush()
    return plan


async def customer_offers(uow: UnitOfWork, customer: User) -> list[Plan]:
    if customer.telegram_id is None:
        return []
    return [
        p
        for p in await uow.plans.list_with_durations()
        if is_premium_plan(p) and can_buy(p, customer)
    ]


def fmt_rub(minor: int) -> str:
    value = minor / 100
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return text.replace(",", " ") + " ₽"


def offer_text(
    plan_name: str, durations: Sequence[tuple[int, int]], bot_username: str, plan_id: int
) -> str:
    prices = ", ".join(f"{days} дн. — {fmt_rub(price)}" for days, price in durations)
    lines = [f"💎 Счёт на премиум-сервер «{plan_name}»", prices]
    if bot_username:
        lines.append(f"Оплатить: {plan_url(bot_username, plan_id)}")
    return "\n\n".join(lines)
