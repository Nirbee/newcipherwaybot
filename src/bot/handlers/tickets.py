"""Support tickets in the bot: create + converse (mirrors admin screen 11).

A user has at most one open/waiting ticket; new messages append to it. Replies from
the cabinet arrive via the admin API (which DMs the user); messages sent here while a
ticket is open are appended and flip the status back to OPEN.
"""

from __future__ import annotations

import contextlib
import uuid
from html import escape as hesc
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from src.application.events import TicketOpened
from src.bot.banners import render_screen
from src.bot.keyboards import simple_keyboard
from src.bot.screen import ack
from src.core.enums import TicketAuthor, TicketStatus
from src.core.logging import get_logger
from src.infrastructure.database.base import utcnow
from src.infrastructure.database.models.ticket import Ticket, TicketMessage
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer

router = Router(name="tickets")
log = get_logger(__name__)

# Same volume the admin API uploads (src/web/routes/admin/uploads.py) writes into and the
# web app serves at /uploads/... — screenshots land in a "tickets" subdir so they show up
# in the admin ticket thread without a dedicated file-serving endpoint.
UPLOAD_DIR = Path("uploads") / "tickets"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # mirrors uploads.py's cap


async def _append_ticket_message(
    container: AppContainer,
    db_user: User,
    *,
    text: str,
    attachment_url: str | None = None,
    attachment_kind: str | None = None,
) -> tuple[int, bool]:
    """Append to the user's open ticket (or open a new one). Returns (ticket_id, created)."""
    async with container.uow() as uow:
        tickets = await uow.tickets.list(user_id=db_user.id)
        active = next((t for t in tickets if t.status is not TicketStatus.CLOSED), None)
        created = False
        if active is None:
            active = Ticket(user_id=db_user.id, subject=(text or "📎 Вложение")[:64])
            await uow.tickets.add(active)
            created = True
        await uow.ticket_messages.add(
            TicketMessage(
                ticket_id=active.id,
                author=TicketAuthor.USER,
                text=text[:4096],
                attachment_url=attachment_url,
                attachment_kind=attachment_kind,
            )
        )
        active.status = TicketStatus.OPEN
        active.updated_at = utcnow()  # same-status assign is not dirty -> force the bump
        await uow.commit()
        return active.id, created


class TicketForm(StatesGroup):
    waiting_text = State()


async def _maybe_ai_reply(
    message: Message, container: AppContainer, db_user: User, ticket_id: int
) -> bool:
    """If AI support answered/escalated the ticket, relay it to the user. Returns handled."""
    try:
        with contextlib.suppress(Exception):
            await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]
        outcome, text = await container.ai_support.handle_ticket(db_user, ticket_id)
        if outcome == "reply" and text:
            await message.answer(text)
            return True
        if outcome == "escalate":
            await message.answer(
                "Приняли обращение — подключаем оператора, ответим здесь в ближайшее время."
            )
            return True
        return False
    except Exception as exc:
        log.warning("ai support reply failed", ticket=ticket_id, error=str(exc))
        return False


async def begin_ticket(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    """Entry from the support action: show the open ticket or start a new one."""
    async with container.uow() as uow:
        open_tickets = await uow.tickets.list(user_id=db_user.id)
        active = next((t for t in open_tickets if t.status is not TicketStatus.CLOSED), None)
    if active is not None:
        text = (
            f"<b>🆘 Тикет #{active.id}</b>\n──────────\n"
            f"Тема: <b>{hesc(active.subject)}</b>\n"
            "Просто напиши сообщение — ответим в этой же переписке."
        )
    else:
        text = (
            "<b>🆘 Новый тикет</b>\n\n"
            "Опиши проблему одним сообщением — создадим тикет и ответим прямо здесь."
        )
    await render_screen(cb, container, "support", text, simple_keyboard([("‹ Меню", "nav:root")]))
    await ack(cb)


@router.message(Command("support"))
async def cmd_support(message: Message, container: AppContainer, db_user: User) -> None:
    await message.answer("Опиши проблему одним сообщением — создадим тикет и ответим здесь.")


@router.message(F.text & ~F.text.startswith("/"))
async def user_message(
    message: Message, container: AppContainer, db_user: User, state: FSMContext
) -> None:
    """Plain text outside flows: append to an open ticket, or open a new one."""
    if await state.get_state() is not None:
        return  # user is mid-FSM (e.g. entering a promocode) — don't hijack their input
    text = (message.text or "").strip()
    if not text:
        return
    async with container.uow() as uow:
        cfg = container.bot_config
        mode = str(await cfg.value(uow, "SUPPORT_MODE"))
        support_chat = str(await cfg.value(uow, "SUPPORT_CHAT_ID") or "")
    if mode != "tickets":
        # Only the in-bot ticket mode consumes free text. Under bot/miniapp/redirect
        # act_support pointed the user elsewhere; creating a DB ticket nobody is watching
        # would silently orphan the message and contradict what we told them.
        return
    ticket_id, created = await _append_ticket_message(container, db_user, text=text)

    if created:
        # Instant "tickets" report topic (screen 14) listens on the bus.
        await container.event_bus.publish(
            TicketOpened(
                ticket_id=ticket_id,
                user_id=db_user.id,
                telegram_id=db_user.telegram_id,
                username=db_user.username,
                subject=text[:64],
            )
        )

    # AI support: if enabled and no human is handling this ticket yet, answer or escalate.
    if not await _maybe_ai_reply(message, container, db_user, ticket_id):
        if created:
            await message.answer(
                f"🆗 Тикет <b>#{ticket_id}</b> создан — ответим здесь.", parse_mode="HTML"
            )
        else:
            await message.answer("Добавил к тикету ✍️")

    # Mirror into the support group when configured.
    if support_chat.lstrip("-").isdigit():
        # Group misconfiguration must not break the user flow.
        with contextlib.suppress(Exception):
            await message.bot.send_message(  # type: ignore[union-attr]
                int(support_chat),
                f"🎫 #{ticket_id} от @{db_user.username or db_user.telegram_id}:\n\n{text[:1000]}",
            )


async def _download_screenshot(message: Message) -> tuple[str, str] | None:
    """Save a photo (or image sent as a file) to uploads/tickets/; None for anything else
    or a file over the size cap. Telegram photos have no filename/mime — always .jpg."""
    file_obj: Any
    kind: str
    ext: str
    if message.photo:
        file_obj = message.photo[-1]
        kind, ext = "photo", ".jpg"
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_obj = message.document
        kind = "document"
        suffix = Path(message.document.file_name or "").suffix.lower()
        ext = suffix if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
    else:
        return None
    if getattr(file_obj, "file_size", None) and file_obj.file_size > MAX_ATTACHMENT_BYTES:
        return None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    await message.bot.download(file_obj, destination=UPLOAD_DIR / name)  # type: ignore[union-attr]
    return f"/uploads/tickets/{name}", kind


@router.message(F.photo | F.document | F.video | F.voice | F.audio | F.animation | F.video_note)
async def user_media(
    message: Message, container: AppContainer, db_user: User, state: FSMContext
) -> None:
    """Attachment in ticket support: a screenshot (photo, or an image sent as a file) is saved
    straight to the ticket so support can see it. Anything else (voice/video/audio/animation/
    non-image documents) — not otherwise renderable in the admin thread — falls back to asking
    the user to describe the issue in words, same as before.

    A screenshot/voice with no caption otherwise matches no handler and the bot looks dead
    (the free-text catch-all only sees ``F.text``). Scoped like ``user_message``: skip when
    mid-FSM (promocode/withdrawal input) and only in the in-bot ticket mode, so it never
    hijacks another flow or a shop whose support lives elsewhere.
    """
    if await state.get_state() is not None:
        return  # user is mid-FSM — don't hijack their attachment
    async with container.uow() as uow:
        cfg = container.bot_config
        mode = str(await cfg.value(uow, "SUPPORT_MODE"))
        support_chat = str(await cfg.value(uow, "SUPPORT_CHAT_ID") or "")
    if mode != "tickets":
        return

    attachment: tuple[str, str] | None = None
    with contextlib.suppress(Exception):
        attachment = await _download_screenshot(message)
    if attachment is None:
        await message.answer(
            "Опиши, пожалуйста, проблему словами — отдельным сообщением или подписью к вложению. "
            "Так мы сразу поймём, чем помочь."
        )
        return

    attachment_url, attachment_kind = attachment
    caption = (message.caption or "").strip()
    ticket_id, created = await _append_ticket_message(
        container,
        db_user,
        text=caption,
        attachment_url=attachment_url,
        attachment_kind=attachment_kind,
    )

    if created:
        await container.event_bus.publish(
            TicketOpened(
                ticket_id=ticket_id,
                user_id=db_user.id,
                telegram_id=db_user.telegram_id,
                username=db_user.username,
                subject=caption[:64] or "📎 Скриншот",
            )
        )

    # AI support only makes sense with actual text to read; an attachment-only message just
    # gets acknowledged (mirrors the text-only branch in user_message).
    handled = bool(caption) and await _maybe_ai_reply(message, container, db_user, ticket_id)
    if not handled:
        if created:
            await message.answer(
                f"🆗 Тикет <b>#{ticket_id}</b> создан — ответим здесь.", parse_mode="HTML"
            )
        else:
            await message.answer("Добавил вложение к тикету ✍️")

    # Mirror into the support group when configured — send the photo itself, not just a note.
    if support_chat.lstrip("-").isdigit():
        with contextlib.suppress(Exception):
            cap = f"🎫 #{ticket_id} от @{db_user.username or db_user.telegram_id}"
            if caption:
                cap += f":\n\n{caption[:900]}"
            if message.photo:
                await message.bot.send_photo(  # type: ignore[union-attr]
                    int(support_chat), message.photo[-1].file_id, caption=cap
                )
            elif message.document:
                await message.bot.send_document(  # type: ignore[union-attr]
                    int(support_chat), message.document.file_id, caption=cap
                )
