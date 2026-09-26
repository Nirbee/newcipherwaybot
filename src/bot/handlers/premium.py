"""«💎 Премиум-сервер»: a personal server in any country + priority human support.

The request becomes a premium ticket (see application/services/premium.py); the conversation
then continues in that ticket, and the admin answers with a personal invoice link
(``/start plan_<id>`` -> the regular duration/payment screens).
"""

from __future__ import annotations

from html import escape as hesc

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from src.application.events import TicketOpened
from src.application.services import premium
from src.bot.banners import render_screen
from src.bot.keyboards import simple_keyboard
from src.bot.screen import ack
from src.core.logging import get_logger
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer

router = Router(name="premium")
log = get_logger(__name__)

INTRO = (
    "<b>💎 Премиум-сервер</b>\n\n"
    "Личный сервер только для тебя — в любой стране и под твою задачу: доступ к Gemini "
    "и другим сервисам, которые не работают из твоей страны, стабильный IP для работы, игры "
    "с минимальным пингом.\n\n"
    "• отдельный сервер, его скорость ни с кем не делится\n"
    "• страну выбираешь сам\n"
    "• приоритетная поддержка — отвечает человек, а не бот\n\n"
    "Оставь заявку: напиши страну и для чего нужен сервер. Мы подберём вариант и пришлём "
    "счёт прямо сюда."
)


class PremiumForm(StatesGroup):
    waiting_request = State()


@router.callback_query(F.data.startswith("act:premium"))
async def act_premium(
    cb: CallbackQuery | Message, container: AppContainer, db_user: User
) -> None:
    async with container.uow() as uow:
        ticket = await premium.active_ticket(uow, db_user.id, premium_only=True)
    if ticket is not None:
        text = (
            f"<b>💎 Заявка #{ticket.id} в работе</b>\n\n"
            "Просто напиши сообщение — оно попадёт в эту переписку, ответим здесь же."
        )
        kb = simple_keyboard([("‹ Меню", "nav:root")])
        await render_screen(cb, container, "premium", text, kb)
    else:
        await render_screen(
            cb,
            container,
            "premium",
            INTRO,
            simple_keyboard([("📝 Оставить заявку", "premium:new"), ("‹ Меню", "nav:root")]),
        )
    await ack(cb)


@router.callback_query(F.data == "premium:new")
async def premium_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PremiumForm.waiting_request)
    if isinstance(cb.message, Message):
        await cb.message.answer(
            "Напиши одним сообщением: <b>в какой стране</b> нужен сервер и <b>для чего</b> "
            "(например: «США, для Gemini и ChatGPT» или «Япония, игры»).",
            parse_mode="HTML",
        )
    await cb.answer()


@router.message(PremiumForm.waiting_request, F.text & ~F.text.startswith("/"))
async def premium_request(
    message: Message, container: AppContainer, db_user: User, state: FSMContext
) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    await state.clear()
    async with container.uow() as uow:
        user = await uow.users.get(db_user.id)
        if user is None:
            return
        ticket, created = await premium.open_request(uow, user, text)
        await uow.commit()
        ticket_id = ticket.id
    if created:
        await container.event_bus.publish(
            TicketOpened(
                ticket_id=ticket_id,
                user_id=db_user.id,
                telegram_id=db_user.telegram_id,
                username=db_user.username,
                subject=f"💎 {text[:60]}",
            )
        )
    who = f"@{db_user.username}" if db_user.username else str(db_user.telegram_id)
    await container.notifier.notify_admins(
        f"💎 Премиум-заявка #{ticket_id} от {who}:\n\n{text[:1000]}"
    )
    log.info("premium request", user=db_user.id, ticket=ticket_id)
    await message.answer(
        f"✅ Заявка <b>#{ticket_id}</b> принята. Ответим здесь в ближайшее время — "
        "все сообщения, которые ты сюда напишешь, попадут в эту переписку.\n\n"
        f"<i>{hesc(text[:300])}</i>",
        parse_mode="HTML",
    )
