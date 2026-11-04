"""Customer support chat — one implementation behind every customer surface.

The mini-app / web cabinet (``/api/cabinet/support``) and the Windows app
(``/api/app/v1/support``) differ only in how they authenticate the customer; the
conversation itself (history, text, screenshot, AI auto-reply) lives here so the two can
never drift apart.

Which conversation is decided by ``Ticket.channel`` + ``Ticket.subscription_id``, never by
the subject: ``app_subscription_id=None`` is the shared bot/cabinet thread of the account;
a subscription id is the Windows-app thread of THAT subscription (its link token is all the
app proved). The «💻 » subject prefix on app tickets is only a visual hint in the admin queue.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from src.application.events import TicketOpened
from src.core.enums import TicketAuthor, TicketChannel, TicketStatus
from src.infrastructure.database.base import utcnow
from src.infrastructure.database.models.ticket import Ticket, TicketMessage
from src.infrastructure.database.models.user import User
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.di import AppContainer

SUPPORT_UPLOADS = Path("uploads") / "tickets"
SUPPORT_IMAGE_MAX = 10 * 1024 * 1024
# multipart overhead allowance on top of the image itself
SUPPORT_PHOTO_BODY_MAX = SUPPORT_IMAGE_MAX + 64 * 1024
APP_TICKET_PREFIX = "💻 "


def image_ext(data: bytes) -> str | None:
    """Extension by the file's own signature — the client-sent Content-Type is not trusted
    (a «PNG» that is really HTML must never land under /uploads)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


class SupportIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


def support_msg(m: Any) -> dict[str, Any]:
    text = m.text[2:].strip() if m.text.startswith("🤖 ") else m.text
    return {
        "from": "you" if m.author is TicketAuthor.USER else "support",
        "text": text,
        "image": m.attachment_url if m.attachment_kind in ("photo", "document") else None,
        "at": m.created_at.isoformat(),
    }


@asynccontextmanager
async def photo_form(request: Request) -> AsyncIterator[tuple[UploadFile, str]]:
    """Parse a screenshot upload (multipart ``file`` + optional ``text``) — call it AFTER auth
    and rate limits, never as a FastAPI body param (those are read before dependencies run).
    A declared, bounded Content-Length is required; at most one file and two fields."""
    length = request.headers.get("content-length", "")
    if not length.isdigit():
        raise HTTPException(411, "length required")
    if int(length) > SUPPORT_PHOTO_BODY_MAX:
        raise HTTPException(413, "file too large")
    form = await request.form(max_files=1, max_fields=2, max_part_size=8192)
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(400, "file is required")
        text = form.get("text")
        yield file, text if isinstance(text, str) else ""
    finally:
        await form.close()


async def _active_ticket(
    uow: UnitOfWork, user_id: int, app_subscription_id: int | None
) -> Ticket | None:
    return next(
        iter(await uow.tickets.open_for(user_id, app_subscription_id=app_subscription_id)),
        None,
    )


async def support_history(
    container: AppContainer, user: User, *, app_subscription_id: int | None = None
) -> dict[str, Any]:
    """Current support conversation (chat view + polling for operator replies)."""
    async with container.uow() as uow:
        active = await _active_ticket(uow, user.id, app_subscription_id)
        if active is None:
            return {"ticket_id": None, "status": None, "messages": []}
        rows = await uow.ticket_messages.list(ticket_id=active.id)
        msgs = sorted(rows, key=lambda m: m.id)  # base DAO list() has no ORDER BY
        return {
            "ticket_id": active.id,
            "status": active.status.value,
            "messages": [support_msg(m) for m in msgs],
        }


async def _post(
    container: AppContainer,
    user: User,
    message: TicketMessage,
    subject: str,
    app_subscription_id: int | None,
) -> tuple[int, bool]:
    """Append ``message`` to the open ticket of this conversation (opening one if none) and
    reopen it. Returns (ticket_id, created); publishes TicketOpened for a new ticket."""
    app = app_subscription_id is not None
    if app:
        subject = APP_TICKET_PREFIX + subject
    subject = subject[:64]
    async with container.uow() as uow:
        active = await _active_ticket(uow, user.id, app_subscription_id)
        created = active is None
        if active is None:
            active = Ticket(
                user_id=user.id,
                subject=subject,
                channel=TicketChannel.APP if app else TicketChannel.CABINET,
                subscription_id=app_subscription_id,
            )
            await uow.tickets.add(active)
        message.ticket_id = active.id
        await uow.ticket_messages.add(message)
        active.status = TicketStatus.OPEN
        active.updated_at = utcnow()
        await uow.commit()
        ticket_id = active.id
    if created:
        await container.event_bus.publish(
            TicketOpened(
                ticket_id=ticket_id,
                user_id=user.id,
                telegram_id=user.telegram_id or 0,
                username=user.username,
                subject=subject,
            )
        )
    return ticket_id, created


async def support_send(
    container: AppContainer,
    user: User,
    raw_text: str,
    *,
    app_subscription_id: int | None = None,
) -> dict[str, Any]:
    """Post a support message; the AI answers/escalates (same engine as the bot tickets).
    In the app channel the AI is pinned to that one subscription (tools act on it only and
    the link is never disclosed)."""
    text = (raw_text or "").strip()[:4096]
    if not text:
        raise HTTPException(400, "empty message")
    message = TicketMessage(author=TicketAuthor.USER, text=text)
    ticket_id, _ = await _post(container, user, message, text, app_subscription_id)
    if app_subscription_id is None:
        outcome, ai_text = await container.ai_support.handle_ticket(user, ticket_id)
    else:
        outcome, ai_text = await container.ai_support.handle_ticket(
            user, ticket_id, subscription_id=app_subscription_id
        )
    return {"ok": True, "ticket_id": ticket_id, "ai_outcome": outcome, "ai_reply": ai_text}


async def support_send_photo(
    container: AppContainer,
    user: User,
    file: UploadFile,
    text: str,
    *,
    app_subscription_id: int | None = None,
) -> dict[str, Any]:
    """A screenshot into the support conversation. Stored like the bot's ticket screenshots
    (uploads/tickets/) so the admin thread renders it inline."""
    data = await file.read(SUPPORT_IMAGE_MAX + 1)
    if len(data) > SUPPORT_IMAGE_MAX:
        raise HTTPException(413, "file too large")
    ext = image_ext(data)
    if ext is None:
        raise HTTPException(400, "unsupported file type")
    name = f"{uuid.uuid4().hex}{ext}"
    try:
        SUPPORT_UPLOADS.mkdir(parents=True, exist_ok=True)
        (SUPPORT_UPLOADS / name).write_bytes(data)
    except OSError as exc:
        raise HTTPException(500, "could not store the file") from exc
    caption = text.strip()[:1000]
    message = TicketMessage(
        author=TicketAuthor.USER,
        text=caption,
        attachment_url=f"/uploads/tickets/{name}",
        attachment_kind="photo",
    )
    ticket_id, _ = await _post(
        container, user, message, caption or "📎 Скриншот", app_subscription_id
    )
    return {"ok": True, "ticket_id": ticket_id}
