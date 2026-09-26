"""Admin: support tickets + channel config (screen 11).

Replying stores the message and (best-effort) delivers it to the user via the bot.
Support channel modes (in-bot tickets / redirect / separate bot / mini-app chat) are
bot-config params surfaced here for the screen's config cards.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from src.application.services import premium
from src.core.enums import TicketAuthor, TicketStatus
from src.core.logging import get_logger
from src.infrastructure.database.base import utcnow
from src.infrastructure.database.models.ticket import TicketMessage
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import OkOut, audit, iso
from src.web.routes.admin.deps import AdminIdentity, require_admin

log = get_logger(__name__)

router = APIRouter()

_CHANNEL_KEYS = ("SUPPORT_MODE", "SUPPORT_REDIRECT_USERNAME")


PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH, PRIORITY_URGENT = 0, 1, 2, 3
# Auto-escalation by how long the customer has been waiting for a staff answer.
_ESCALATE_HIGH_MIN = 4 * 60
_ESCALATE_URGENT_MIN = 12 * 60
_UPLOADS = Path("uploads")


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def effective_priority(manual: int, waiting_min: int | None, is_premium: bool) -> int:
    """The queue's sort key: the higher of the manual priority and the waiting-time
    escalation, bumped one level for premium customers (their support is the product)."""
    auto = PRIORITY_NORMAL
    if waiting_min is not None:
        if waiting_min >= _ESCALATE_URGENT_MIN:
            auto = PRIORITY_URGENT
        elif waiting_min >= _ESCALATE_HIGH_MIN:
            auto = PRIORITY_HIGH
    level = max(manual, auto)
    if is_premium:
        level += 1
    return min(level, PRIORITY_URGENT)


@router.get("/tickets")
async def list_tickets(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    from sqlalchemy import func, select

    from src.infrastructure.database.models.ticket import Ticket
    from src.infrastructure.database.models.user import User

    now = dt.datetime.now(dt.UTC)
    async with container.uow() as uow:
        counts = (
            select(TicketMessage.ticket_id, func.count().label("cnt"))
            .group_by(TicketMessage.ticket_id)
            .subquery()
        )
        stmt = (
            select(Ticket, User.username, func.coalesce(counts.c.cnt, 0))
            .join(User, User.id == Ticket.user_id)
            .outerjoin(counts, counts.c.ticket_id == Ticket.id)
            .order_by(Ticket.updated_at.desc())
            .limit(200)
        )
        tickets = (await uow.session.execute(stmt)).all()
        ids = [t.id for t, _u, _c in tickets]
        last_ids = select(func.max(TicketMessage.id)).where(TicketMessage.ticket_id.in_(ids))
        last_ids = last_ids.group_by(TicketMessage.ticket_id)
        last_msgs = {
            m.ticket_id: m
            for m in (
                await uow.session.scalars(
                    select(TicketMessage).where(TicketMessage.id.in_(last_ids))
                )
            ).all()
        } if ids else {}

        rows = []
        for t, username, cnt in tickets:
            last = last_msgs.get(t.id)
            waiting = None
            if (
                t.status is not TicketStatus.CLOSED
                and last is not None
                and last.author is TicketAuthor.USER
            ):
                waiting = max(0, int((now - _aware(last.created_at)).total_seconds() // 60))
            rows.append(
                {
                    "id": t.id,
                    "user_id": t.user_id,
                    "username": username,
                    "subject": t.subject,
                    "status": t.status.value,
                    "is_premium": t.is_premium,
                    "priority": t.priority,
                    "effective_priority": effective_priority(t.priority, waiting, t.is_premium),
                    "waiting_minutes": waiting,
                    "messages": int(cnt),
                    "updated_at": iso(t.updated_at),
                }
            )
        # Open first; within open: effective priority, then the longest wait.
        rows.sort(
            key=lambda r: (
                r["status"] == TicketStatus.CLOSED.value,
                -r["effective_priority"],
                -(r["waiting_minutes"] or 0),
            )
        )
        open_count = await uow.tickets.open_count()
    return {"items": rows[:100], "open_count": open_count}


@router.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: int, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    async with container.uow() as uow:
        t = await uow.tickets.get(ticket_id)
        if t is None:
            raise HTTPException(404, "ticket not found")
        user = await uow.users.get(t.user_id)
        messages = sorted(await uow.ticket_messages.list(ticket_id=ticket_id), key=lambda m: m.id)
        offers = await premium.customer_offers(uow, user) if user is not None else []
        return {
            "id": t.id,
            "subject": t.subject,
            "status": t.status.value,
            "is_premium": t.is_premium,
            "priority": t.priority,
            "offers": [
                {
                    "id": o.id,
                    "name": o.name,
                    "is_active": o.is_active,
                    "durations": [
                        {
                            "days": d.days,
                            "price_minor": next(
                                (pr.price_minor for pr in d.prices if pr.currency.value == "RUB"),
                                None,
                            ),
                        }
                        for d in o.durations
                    ],
                }
                for o in offers
            ],
            "user": {
                "id": t.user_id,
                "username": user.username if user else None,
                "telegram_id": user.telegram_id if user else None,
            },
            "messages": [
                {
                    "id": m.id,
                    "author": m.author.value,
                    "text": m.text,
                    "attachment_url": m.attachment_url,
                    "attachment_kind": m.attachment_kind,
                    "at": iso(m.created_at),
                }
                for m in messages
            ],
        }


class ReplyIn(BaseModel):
    text: str = Field("", max_length=4096)
    # A screenshot uploaded via /api/admin/upload (served under /uploads/...).
    attachment_url: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _something(self) -> ReplyIn:
        if not self.text.strip() and not self.attachment_url:
            raise ValueError("empty reply")
        return self


def _upload_path(url: str) -> Path:
    """Map an /uploads/... url to its file, refusing anything outside the uploads dir."""
    if not url.startswith("/uploads/") or ".." in url:
        raise HTTPException(400, "attachment must be an uploaded file")
    path = _UPLOADS / url.removeprefix("/uploads/")
    if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp") or not path.is_file():
        raise HTTPException(400, "attachment must be an uploaded image")
    return path


@router.post("/tickets/{ticket_id}/reply")
async def reply_ticket(
    ticket_id: int,
    body: ReplyIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    photo = _upload_path(body.attachment_url) if body.attachment_url else None
    text = body.text.strip()
    async with container.uow() as uow:
        t = await uow.tickets.get(ticket_id)
        if t is None:
            raise HTTPException(404, "ticket not found")
        user = await uow.users.get(t.user_id)
        await uow.ticket_messages.add(
            TicketMessage(
                ticket_id=ticket_id,
                author=TicketAuthor.ADMIN,
                text=text,
                attachment_url=body.attachment_url if photo else None,
                attachment_kind="photo" if photo else None,
            )
        )
        t.status = TicketStatus.WAITING
        await audit(uow, identity, "ticket.reply", f"ticket:{ticket_id}", photo=bool(photo))
        await uow.commit()

    # Best-effort delivery to the user's chat (bot process not required).
    if user is not None and user.telegram_id:
        try:
            from aiogram import Bot
            from aiogram.types import FSInputFile

            header = f"💬 Ответ поддержки по тикету #{ticket_id}"
            bot = Bot(token=container.settings.bot.token)
            try:
                if photo is not None:
                    caption = f"{header}:\n\n{text}" if text else header
                    await bot.send_photo(
                        user.telegram_id, FSInputFile(photo), caption=caption[:1024]
                    )
                else:
                    await bot.send_message(user.telegram_id, f"{header}:\n\n{text}")
            finally:
                await bot.session.close()
        except Exception as exc:
            log.warning("ticket reply delivery failed", ticket_id=ticket_id, error=str(exc))
    return OkOut()


class PriorityIn(BaseModel):
    priority: int = Field(..., ge=PRIORITY_LOW, le=PRIORITY_URGENT)


@router.patch("/tickets/{ticket_id}/priority")
async def set_ticket_priority(
    ticket_id: int,
    body: PriorityIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        t = await uow.tickets.get(ticket_id)
        if t is None:
            raise HTTPException(404, "ticket not found")
        t.priority = body.priority
        await audit(uow, identity, "ticket.priority", f"ticket:{ticket_id}", priority=body.priority)
        await uow.commit()
    return OkOut()


class StatusIn(BaseModel):
    status: TicketStatus


@router.patch("/tickets/{ticket_id}/status")
async def set_ticket_status(
    ticket_id: int,
    body: StatusIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        t = await uow.tickets.get(ticket_id)
        if t is None:
            raise HTTPException(404, "ticket not found")
        t.status = body.status
        t.closed_at = utcnow() if body.status is TicketStatus.CLOSED else None
        await audit(uow, identity, "ticket.status", f"ticket:{ticket_id}", status=body.status.value)
        await uow.commit()
    return OkOut()


class OfferDurationIn(BaseModel):
    days: int = Field(..., ge=1, le=3650)
    price_minor: int = Field(..., ge=0, le=100_000_000)


class PremiumOfferIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(None, max_length=1024)
    durations: list[OfferDurationIn] = Field(..., min_length=1, max_length=8)
    internal_squads: list[str] = Field(default_factory=list, max_length=20)
    device_limit: int | None = Field(None, ge=0, le=100)
    traffic_limit_gb: int = Field(0, ge=0, le=1_000_000)


@router.post("/tickets/{ticket_id}/premium-offer")
async def premium_offer(
    ticket_id: int,
    body: PremiumOfferIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Invoice a personal server: a private PREMIUM plan for this ticket's customer, posted
    into the conversation and DM'd as a pay link (``/start plan_<id>``)."""
    durations = [(d.days, d.price_minor) for d in body.durations]
    async with container.uow() as uow:
        t = await uow.tickets.get(ticket_id)
        if t is None:
            raise HTTPException(404, "ticket not found")
        customer = await uow.users.get(t.user_id)
        if customer is None or customer.telegram_id is None:
            raise HTTPException(400, "the customer has no Telegram account to pay from")
        plan = await premium.create_offer(
            uow,
            customer=customer,
            name=body.name.strip(),
            durations=durations,
            internal_squads=body.internal_squads,
            device_limit=body.device_limit,
            traffic_limit_gb=body.traffic_limit_gb,
            description=(body.description or "").strip() or None,
        )
        bot_username = str(await container.bot_config.value(uow, "BOT_USERNAME") or "").lstrip("@")
        text = premium.offer_text(plan.name, durations, bot_username, plan.id)
        await uow.ticket_messages.add(
            TicketMessage(ticket_id=ticket_id, author=TicketAuthor.ADMIN, text=text)
        )
        t.is_premium = True
        t.status = TicketStatus.WAITING
        t.updated_at = utcnow()
        await audit(
            uow, identity, "ticket.premium_offer", f"ticket:{ticket_id}",
            plan_id=plan.id, durations=durations,
        )
        await uow.commit()
        plan_id, telegram_id = plan.id, customer.telegram_id
    await container.notifier.notify_user(telegram_id, text)
    return {"ok": True, "plan_id": plan_id, "pay_url": premium.plan_url(bot_username, plan_id)}


@router.get("/support-channels")
async def get_support_channels(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        cfg = container.bot_config
        mode = await cfg.value(uow, "SUPPORT_MODE")
        redirect = await cfg.value(uow, "SUPPORT_REDIRECT_USERNAME")
    return {"mode": mode, "redirect_username": redirect}


class ChannelsIn(BaseModel):
    mode: str | None = Field(None, pattern="^(tickets|redirect|miniapp)$")
    redirect_username: str | None = Field(None, max_length=64)


@router.patch("/support-channels")
async def patch_support_channels(
    body: ChannelsIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    changes: dict[str, Any] = {}
    if body.mode is not None:
        changes["SUPPORT_MODE"] = body.mode
    if body.redirect_username is not None:
        changes["SUPPORT_REDIRECT_USERNAME"] = body.redirect_username.lstrip("@")
    if not changes:
        raise HTTPException(400, "no changes")
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, changes)
        await audit(uow, identity, "support.channels", None, keys=sorted(changes))
        await uow.commit()
    return OkOut()
