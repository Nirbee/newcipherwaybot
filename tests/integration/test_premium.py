"""Premium personal servers: bot request -> premium ticket -> admin invoice (private plan) ->
only its addressee can buy it, at the quoted price.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import httpx
import pytest

from src.application.dto.pricing import PurchaseRequest
from src.application.services import premium
from src.core.enums import Currency, PlanCategory
from src.core.exceptions import PurchaseError
from tests.factories import make_plan, make_user
from tests.integration.test_admin_api import ApiTestContainer, _login, client  # noqa: F401


class _Recorder:
    def __init__(self) -> None:
        self.users: list[tuple[int, str]] = []
        self.admins: list[str] = []

    async def notify_user(self, telegram_id: int, text: str) -> bool:
        self.users.append((telegram_id, text))
        return True

    async def notify_admins(self, text: str, *, topic: str | None = None) -> None:
        self.admins.append(text)

    async def notify_admins_document(self, document: object, *, caption: str | None = None) -> None:
        ...

    async def aclose(self) -> None: ...


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.caption = None
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append(text)


class _FakeState:
    def __init__(self) -> None:
        self.state: object = None

    async def clear(self) -> None:
        self.state = None

    async def get_state(self) -> object:
        return self.state


async def _customer(container: ApiTestContainer, tg: int, username: str = "gemini_fan") -> int:
    async with container.uow() as uow:
        user = await make_user(uow, telegram_id=tg, username=username)
        await uow.commit()
        return user.id


async def _request_from_bot(container: ApiTestContainer, user_id: int, text: str) -> None:
    from src.bot.handlers import premium as premium_handler

    async with container.uow() as uow:
        user = await uow.users.get(user_id)
    await premium_handler.premium_request(
        _FakeMessage(text), container, user, _FakeState()  # type: ignore[arg-type]
    )


async def test_bot_request_opens_premium_ticket_and_pings_admins(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    rec = _Recorder()
    container.notifier = rec
    uid = await _customer(container, 7001)
    await _request_from_bot(container, uid, "США, для Gemini")

    async with container.uow() as uow:
        ticket = await premium.active_ticket(uow, uid, premium_only=True)
    assert ticket is not None and ticket.is_premium
    assert "Gemini" in ticket.subject
    assert rec.admins and "Премиум-заявка" in rec.admins[0]


async def test_admin_queue_puts_open_premium_tickets_first(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.notifier = _Recorder()
    uid = await _customer(container, 7002)
    await _request_from_bot(container, uid, "Япония, игры")
    # a newer ordinary ticket from someone else
    from src.core.enums import TicketStatus
    from src.infrastructure.database.models.ticket import Ticket

    async with container.uow() as uow:
        other = await make_user(uow, telegram_id=7003)
        await uow.tickets.add(Ticket(user_id=other.id, subject="обычный", status=TicketStatus.OPEN))
        await uow.commit()

    items = (await http.get("/api/admin/tickets", headers=await _login(http))).json()["items"]
    assert items[0]["is_premium"] is True
    assert items[1]["is_premium"] is False


async def test_invoice_creates_private_plan_and_sends_pay_link(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    rec = _Recorder()
    container.notifier = rec
    auth = await _login(http)
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, {"BOT_USERNAME": "cipherway_bot"})
        await uow.commit()
    uid = await _customer(container, 7004)
    await _request_from_bot(container, uid, "США, Gemini")
    async with container.uow() as uow:
        ticket = await premium.active_ticket(uow, uid, premium_only=True)

    res = await http.post(
        f"/api/admin/tickets/{ticket.id}/premium-offer",
        headers=auth,
        json={
            "name": "Премиум США",
            "durations": [{"days": 30, "price_minor": 250000}, {"days": 90, "price_minor": 700000}],
            "internal_squads": ["squad-usa"],
            "device_limit": 5,
        },
    )
    assert res.status_code == 200, res.text
    plan_id = res.json()["plan_id"]
    assert res.json()["pay_url"] == f"https://t.me/cipherway_bot?start=plan_{plan_id}"
    assert rec.users[-1][0] == 7004 and f"plan_{plan_id}" in rec.users[-1][1]

    async with container.uow() as uow:
        plan = await uow.plans.get(plan_id)
    assert plan.category is PlanCategory.PREMIUM
    assert plan.allowed_telegram_ids == [7004]
    assert plan.internal_squads == ["squad-usa"]

    detail = (await http.get(f"/api/admin/tickets/{ticket.id}", headers=auth)).json()
    assert detail["is_premium"] is True
    assert [o["id"] for o in detail["offers"]] == [plan_id]
    assert "Счёт на премиум-сервер" in detail["messages"][-1]["text"]


async def test_premium_plan_only_for_its_addressee_and_undiscounted(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.notifier = _Recorder()
    owner_id = await _customer(container, 7005)
    stranger_id = await _customer(container, 7006, username="stranger")
    async with container.uow() as uow:
        owner = await uow.users.get(owner_id)
        owner.personal_discount_pct = 20
        plan = await premium.create_offer(
            uow,
            customer=owner,
            name="Премиум Германия",
            durations=[(30, 150000)],
            internal_squads=[],
            device_limit=3,
            traffic_limit_gb=0,
            description=None,
        )
        await uow.commit()

        def req(uid: int) -> PurchaseRequest:
            return PurchaseRequest(
                user_id=uid, plan_id=plan.id, duration_days=30, currency=Currency.RUB
            )

        quote = await container.pricing.quote(uow, req(owner_id))
        assert quote.final.amount_minor == 150000  # the personal discount doesn't apply
        with pytest.raises(PurchaseError):
            await container.pricing.quote(uow, req(stranger_id))


async def test_premium_plans_stay_out_of_the_storefront(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    from src.bot.handlers import purchase

    http, container = client
    uid = await _customer(container, 7007)
    async with container.uow() as uow:
        await make_plan(uow, code="public", name="Публичный")
        owner = await uow.users.get(uid)
        await premium.create_offer(
            uow, customer=owner, name="Личный", durations=[(30, 100000)],
            internal_squads=[], device_limit=1, traffic_limit_gb=0, description=None,
        )
        await uow.commit()

    shown: list[str] = []

    async def capture(cb, container, key, caption, markup):  # type: ignore[no-untyped-def]
        shown.extend(b.text for row in markup.inline_keyboard for b in row)

    orig = purchase.render_screen
    purchase.render_screen = capture  # type: ignore[assignment]
    try:
        async with container.uow() as uow:
            owner = await uow.users.get(uid)

        async def _true(*a: object, **k: object) -> bool:
            return True

        orig_lock = purchase.ensure_channel
        purchase.ensure_channel = _true  # type: ignore[assignment]
        try:
            await purchase.show_plans(_FakeMessage(), container, owner)  # type: ignore[arg-type]
        finally:
            purchase.ensure_channel = orig_lock  # type: ignore[assignment]
    finally:
        purchase.render_screen = orig  # type: ignore[assignment]
    assert any("Публичный" in s for s in shown)
    assert not any("Личный" in s for s in shown)


async def test_premium_conversation_works_even_outside_ticket_mode(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    from src.bot.handlers import tickets

    http, container = client
    container.notifier = _Recorder()
    uid = await _customer(container, 7008)
    await _request_from_bot(container, uid, "Нидерланды, работа")
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, {"SUPPORT_MODE": "redirect"})
        await uow.commit()
        user = await uow.users.get(uid)

    msg = _FakeMessage("а можно с выделенным IP?")
    await tickets.user_message(msg, container, user, _FakeState())  # type: ignore[arg-type]

    async with container.uow() as uow:
        ticket = await premium.active_ticket(uow, uid, premium_only=True)
        messages = await uow.ticket_messages.list(ticket_id=ticket.id)
    assert [m.text for m in messages][-1] == "а можно с выделенным IP?"
