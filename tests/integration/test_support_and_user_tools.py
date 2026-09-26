"""Support queue priorities, screenshots both ways, and the user-card money/HWID tools.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx

from src.application.dto.pricing import PurchaseRequest
from src.core.enums import Currency, TicketAuthor, TicketStatus
from src.infrastructure.database.models.ticket import Ticket, TicketMessage
from src.web.routes.admin.tickets import effective_priority
from tests.factories import make_plan, make_user
from tests.integration.test_admin_api import (  # noqa: F401
    ApiTestContainer,
    _login,
    _tma_headers,
    client,
)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


def test_effective_priority_escalates_with_waiting_time_and_premium() -> None:
    assert effective_priority(1, None, False) == 1
    assert effective_priority(1, 30, False) == 1
    assert effective_priority(1, 5 * 60, False) == 2
    assert effective_priority(1, 13 * 60, False) == 3
    assert effective_priority(0, 5 * 60, False) == 2  # waiting beats a low manual priority
    assert effective_priority(3, None, False) == 3
    assert effective_priority(1, None, True) == 2  # premium support jumps a level
    assert effective_priority(3, 20 * 60, True) == 3  # capped


async def test_queue_orders_by_priority_then_longest_wait(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    auth = await _login(http)
    now = dt.datetime.now(dt.UTC)
    async with container.uow() as uow:
        ids = {}
        for tg, name, waited_h in ((1, "fresh", 0.2), (2, "old", 6), (3, "ancient", 14)):
            u = await make_user(uow, telegram_id=770000 + tg)
            t = Ticket(user_id=u.id, subject=name, status=TicketStatus.OPEN)
            await uow.tickets.add(t)
            await uow.ticket_messages.add(
                TicketMessage(
                    ticket_id=t.id,
                    author=TicketAuthor.USER,
                    text=name,
                    created_at=now - dt.timedelta(hours=waited_h),
                )
            )
            ids[name] = t.id
        closed = Ticket(user_id=u.id, subject="closed", status=TicketStatus.CLOSED, priority=3)
        await uow.tickets.add(closed)
        await uow.commit()

    items = (await http.get("/api/admin/tickets", headers=auth)).json()["items"]
    assert [i["subject"] for i in items] == ["ancient", "old", "fresh", "closed"]
    assert items[0]["effective_priority"] == 3 and items[0]["waiting_minutes"] >= 14 * 60

    # A manual "urgent" lifts the fresh ticket above "old"; it ties with the auto-urgent
    # "ancient" one, and ties go to whoever has waited longer.
    res = await http.patch(
        f"/api/admin/tickets/{ids['fresh']}/priority", headers=auth, json={"priority": 3}
    )
    assert res.status_code == 200
    items = (await http.get("/api/admin/tickets", headers=auth)).json()["items"]
    assert [i["subject"] for i in items][:3] == ["ancient", "fresh", "old"]


async def test_admin_reply_with_screenshot_is_stored_on_the_thread(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    auth = await _login(http)
    uploaded = (
        await http.post(
            "/api/admin/upload", headers=auth, files={"file": ("shot.png", PNG, "image/png")}
        )
    ).json()["url"]
    try:
        async with container.uow() as uow:
            u = await make_user(uow, telegram_id=7700001)
            t = Ticket(user_id=u.id, subject="help", status=TicketStatus.OPEN)
            await uow.tickets.add(t)
            await uow.commit()
            ticket_id = t.id

        res = await http.post(
            f"/api/admin/tickets/{ticket_id}/reply",
            headers=auth,
            json={"text": "Вот где нажать", "attachment_url": uploaded},
        )
        assert res.status_code == 200, res.text
        msgs = (await http.get(f"/api/admin/tickets/{ticket_id}", headers=auth)).json()["messages"]
        assert msgs[-1]["attachment_url"] == uploaded and msgs[-1]["attachment_kind"] == "photo"

        # a path outside uploads is refused
        bad = await http.post(
            f"/api/admin/tickets/{ticket_id}/reply",
            headers=auth,
            json={"text": "x", "attachment_url": "/uploads/../.env"},
        )
        assert bad.status_code == 400
        empty = await http.post(
            f"/api/admin/tickets/{ticket_id}/reply", headers=auth, json={"text": " "}
        )
        assert empty.status_code == 422
    finally:
        Path("uploads", uploaded.removeprefix("/uploads/")).unlink(missing_ok=True)


async def test_miniapp_customer_can_send_a_screenshot(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, _ = client
    tma = _tma_headers(tg_id=880000111)
    await http.get("/api/cabinet/me", headers=tma)
    res = await http.post(
        "/api/cabinet/support/photo",
        headers=tma,
        files={"file": ("screen.png", PNG, "image/png")},
        data={"text": "не работает"},
    )
    assert res.status_code == 200, res.text
    history = (await http.get("/api/cabinet/support", headers=tma)).json()
    image = history["messages"][-1]["image"]
    try:
        assert image.startswith("/uploads/tickets/")
        assert history["messages"][-1]["text"] == "не работает"
        wrong = await http.post(
            "/api/cabinet/support/photo",
            headers=tma,
            files={"file": ("x.txt", b"hello", "text/plain")},
        )
        assert wrong.status_code == 400
    finally:
        Path(image.lstrip("/")).unlink(missing_ok=True)


async def test_balance_debit_cannot_go_negative_and_hwid_takes_exact_value(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    auth = await _login(http)
    me = await http.get("/api/cabinet/me", headers=_tma_headers(990001))
    user_id = me.json()["user"]["id"]
    await http.post(
        f"/api/admin/users/{user_id}/balance", headers=auth, json={"amount_minor": 30000}
    )
    too_much = await http.post(
        f"/api/admin/users/{user_id}/balance", headers=auth, json={"amount_minor": -50000}
    )
    assert too_much.status_code == 400
    ok = await http.post(
        f"/api/admin/users/{user_id}/balance", headers=auth, json={"amount_minor": -10000}
    )
    assert ok.status_code == 200
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        assert user.balance_minor == 20000
        plan, _ = await make_plan(uow, code="hwid-plan", device_limit=3)
        await uow.commit()
        req = PurchaseRequest(
            user_id=user.id, plan_id=plan.id, duration_days=30, currency=Currency.RUB
        )
        await container.subscriptions.grant(uow, user=user, plan=plan, req=req)
        await uow.commit()

    res = await http.post(f"/api/admin/users/{user_id}/hwid", headers=auth, json={"value": 12})
    assert res.status_code == 200, res.text
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        sub = await uow.subscriptions.get(user.current_subscription_id)
    assert sub.device_limit == 12


def test_report_group_id_normalization() -> None:
    from src.infrastructure.services.reports import normalize_group_id

    assert normalize_group_id("3914340224") == "-1003914340224"  # bare supergroup id
    assert normalize_group_id(" -1003914340224 ") == "-1003914340224"
    assert normalize_group_id("-4012345678") == "-4012345678"  # basic group: kept as-is
    assert normalize_group_id("") == ""


async def test_saving_report_group_normalizes_and_runs_the_delivery_check(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: object
) -> None:
    import pytest

    from src.infrastructure.services import reports

    http, container = client
    seen: dict[str, object] = {}

    async def fake_verify(token: str, group_id: str, topics: list[tuple[str, int | None]]):
        seen["group_id"], seen["topics"] = group_id, topics
        return {"ok": True, "group_id": group_id, "title": "Отчёты", "topics": []}

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr(reports, "verify_report_group", fake_verify)
    auth = await _login(http)
    await http.get("/api/admin/report-topics", headers=auth)  # seeds the topic kinds
    res = await http.post(
        "/api/admin/report-topics/group", headers=auth, json={"group_id": "3914340224"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["title"] == "Отчёты"
    assert seen["group_id"] == "-1003914340224"
    async with container.uow() as uow:
        stored = await container.bot_config.value(uow, "REPORT_GROUP_ID")
    assert stored == "-1003914340224"


async def test_miniapp_assets_are_versioned_and_revalidated(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    import re

    http, _ = client
    page = await http.get("/app/")
    assert page.status_code == 200
    assert page.headers["cache-control"].startswith("no-cache")
    m = re.search(r'src="app\.js\?v=([0-9a-f]{10})"', page.text)
    assert m, "app.js must carry a content-hash version"
    assert re.search(r'href="app\.css\?v=[0-9a-f]{10}"', page.text)
    js = await http.get(f"/app/app.js?v={m.group(1)}")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    web = await http.get("/web/")
    assert re.search(r'src="app\.js\?v=[0-9a-f]{10}"', web.text)


async def test_bot_reply_joins_open_ticket_in_miniapp_mode(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    from src.bot.handlers import tickets

    class _Msg:
        def __init__(self, text: str) -> None:
            self.text = text
            self.answers: list[str] = []
            self.bot = None

        async def answer(self, text: str, **kwargs: object) -> None:
            self.answers.append(text)

    class _State:
        async def get_state(self) -> None:
            return None

    http, container = client
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, {"SUPPORT_MODE": "miniapp"})
        user = await make_user(uow, telegram_id=7712345)
        t = Ticket(user_id=user.id, subject="не грузит", status=TicketStatus.WAITING)
        await uow.tickets.add(t)
        await uow.commit()
        ticket_id = t.id

    await tickets.user_message(_Msg("да, перезагрузил — не помогло"), container, user, _State())  # type: ignore[arg-type]

    async with container.uow() as uow:
        msgs = await uow.ticket_messages.list(ticket_id=ticket_id)
        ticket = await uow.tickets.get(ticket_id)
    assert [m.text for m in msgs] == ["да, перезагрузил — не помогло"]
    assert ticket.status is TicketStatus.OPEN


async def test_bot_ignores_free_text_in_miniapp_mode_without_open_ticket(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    from src.bot.handlers import tickets

    class _Msg:
        text = "привет"
        bot = None

        async def answer(self, text: str, **kwargs: object) -> None: ...

    class _State:
        async def get_state(self) -> None:
            return None

    http, container = client
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, {"SUPPORT_MODE": "miniapp"})
        user = await make_user(uow, telegram_id=7712346)
        await uow.commit()
    await tickets.user_message(_Msg(), container, user, _State())  # type: ignore[arg-type]
    async with container.uow() as uow:
        assert await uow.tickets.list(user_id=user.id) == []
