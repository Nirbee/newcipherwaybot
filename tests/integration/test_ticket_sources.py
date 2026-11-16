"""Where a support message came from (TicketMessage.source) and the admin queue's source tabs.

The mini-app and the web cabinet call the same /api/cabinet/support: the server tells them
apart by the auth that was accepted (Telegram initData vs a web session), never by anything
in the request. Reuses the ApiTestContainer/client fixture from test_admin_api.py.
"""

# ruff: noqa: RUF001  — RU support messages are asserted verbatim.

from __future__ import annotations

from pathlib import Path

import httpx

from src.core.enums import TicketAuthor, TicketSource, TicketStatus
from src.infrastructure.database.models.ticket import Ticket, TicketMessage
from src.web.routes.cabinet_auth import _access_token
from tests.factories import make_user
from tests.integration.test_admin_api import ApiTestContainer, _login, _tma_headers, client
from tests.integration.test_app_api import _FakeAi, _h, _sub
from tests.integration.test_support_and_user_tools import PNG

_FIXTURES = (client,)  # the shared ASGI-client fixture, re-exported (keeps ruff F401/F811 quiet)


async def _web_headers(container: ApiTestContainer, user_id: int) -> dict[str, str]:
    """A web-cabinet session (Bearer access JWT) for an existing account."""
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, {"WEB_CABINET_ENABLED": True})
        await uow.commit()
        user = await uow.users.get(user_id)
    assert user is not None
    return {"Authorization": f"Bearer {_access_token(container, user)}"}


async def _sources(container: ApiTestContainer, ticket_id: int) -> list[TicketSource | None]:
    async with container.uow() as uow:
        rows = sorted(await uow.ticket_messages.list(ticket_id=ticket_id), key=lambda m: m.id)
    return [m.source for m in rows if m.author is TicketAuthor.USER]


async def test_miniapp_and_site_share_the_thread_but_are_told_apart_by_auth(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.ai_support = _FakeAi(container)  # type: ignore[attr-defined]
    tma = _tma_headers(tg_id=880000222)
    user_id = (await http.get("/api/cabinet/me", headers=tma)).json()["user"]["id"]
    web = await _web_headers(container, user_id)

    # a client-chosen «source» is ignored: the auth decides
    first = await http.post(
        "/api/cabinet/support", headers=tma, json={"text": "из мини-аппа", "source": "web"}
    )
    assert first.status_code == 200, first.text
    second = await http.post(
        "/api/cabinet/support", headers=web, json={"text": "с сайта", "source": "miniapp"}
    )
    assert second.status_code == 200, second.text
    ticket_id = first.json()["ticket_id"]
    assert second.json()["ticket_id"] == ticket_id  # one shared thread per account

    photo = await http.post(
        "/api/cabinet/support/photo", headers=web, files={"file": ("s.png", PNG, "image/png")}
    )
    assert photo.status_code == 200, photo.text
    try:
        assert await _sources(container, ticket_id) == [
            TicketSource.MINIAPP,
            TicketSource.WEB,
            TicketSource.WEB,
        ]
        # the web chat reads the same history the mini-app does
        history = (await http.get("/api/cabinet/support", headers=web)).json()
        assert [m["text"] for m in history["messages"] if m["from"] == "you"][:2] == [
            "из мини-аппа",
            "с сайта",
        ]

        auth = await _login(http)
        body = (await http.get("/api/admin/tickets", headers=auth)).json()
        row = next(i for i in body["items"] if i["id"] == ticket_id)
        assert row["sources"] == ["miniapp", "web"]
        assert body["open_by_source"] == {"bot": 0, "miniapp": 1, "web": 1, "app": 0}
        for src in ("miniapp", "web"):
            listed = (await http.get(f"/api/admin/tickets?source={src}", headers=auth)).json()
            assert [i["id"] for i in listed["items"]] == [ticket_id]
        assert (await http.get("/api/admin/tickets?source=bot", headers=auth)).json()["items"] == []

        detail = (await http.get(f"/api/admin/tickets/{ticket_id}", headers=auth)).json()
        assert detail["sources"] == ["miniapp", "web"]
        assert [m["source"] for m in detail["messages"] if m["author"] == "user"] == [
            "miniapp",
            "web",
            "web",
        ]
        assert all(m["source"] is None for m in detail["messages"] if m["author"] == "admin")
    finally:
        async with container.uow() as uow:
            for m in await uow.ticket_messages.list(ticket_id=ticket_id):
                if m.attachment_url:
                    Path(m.attachment_url.lstrip("/")).unlink(missing_ok=True)


async def test_bot_and_windows_app_messages_are_tagged(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    from src.bot.handlers import tickets

    http, container = client
    container.ai_support = _FakeAi(container)  # type: ignore[attr-defined]
    user_id, _ = await _sub(container, "SrcTok12345678", tg=5500301)
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
    assert user is not None
    bot_ticket, _, _ = await tickets._append_ticket_message(container, user, text="из бота")
    res = await http.post("/api/app/v1/support", headers=_h("SrcTok12345678"), json={"text": "q"})
    app_ticket = res.json()["ticket_id"]

    assert await _sources(container, bot_ticket) == [TicketSource.BOT]
    assert await _sources(container, app_ticket) == [TicketSource.APP]
    auth = await _login(http)
    body = (await http.get("/api/admin/tickets", headers=auth)).json()
    assert body["open_by_source"] == {"bot": 1, "miniapp": 0, "web": 0, "app": 1}
    assert [i["id"] for i in body["items"] if i["sources"] == ["app"]] == [app_ticket]
    app_only = (await http.get("/api/admin/tickets?source=app", headers=auth)).json()["items"]
    assert [i["id"] for i in app_only] == [app_ticket]


async def test_source_filter_combines_with_premium_and_counters_skip_closed(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    async with container.uow() as uow:
        ids: dict[str, int] = {}
        for tg, name, prem, status in (
            (1, "web-premium", True, TicketStatus.OPEN),
            (2, "web-regular", False, TicketStatus.WAITING),
            (3, "web-closed", False, TicketStatus.CLOSED),
        ):
            u = await make_user(uow, telegram_id=7710000 + tg)
            t = Ticket(user_id=u.id, subject=name, status=status, is_premium=prem)
            await uow.tickets.add(t)
            await uow.ticket_messages.add(
                TicketMessage(
                    ticket_id=t.id, author=TicketAuthor.USER, text=name, source=TicketSource.WEB
                )
            )
            ids[name] = t.id
        await uow.commit()

    auth = await _login(http)

    async def listed(query: str) -> list[int]:
        res = await http.get(f"/api/admin/tickets?{query}", headers=auth)
        assert res.status_code == 200, res.text
        return sorted(i["id"] for i in res.json()["items"])

    assert await listed("source=web") == sorted(ids.values())
    assert await listed("source=web&premium=true") == [ids["web-premium"]]
    assert await listed("source=web&premium=false") == sorted(
        [ids["web-regular"], ids["web-closed"]]
    )
    body = (await http.get("/api/admin/tickets", headers=auth)).json()
    assert body["open_by_source"]["web"] == 2  # the closed one is not «open»
    bad = await http.get("/api/admin/tickets?source=telegram", headers=auth)
    assert bad.status_code == 422


async def test_site_and_web_cabinet_get_the_support_contacts(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    landing = (await http.get("/api/cabinet/public/landing")).json()
    assert landing["support_telegram"] == ""
    async with container.uow() as uow:
        await container.bot_config.set_values(
            uow,
            {
                "WEB_CABINET_ENABLED": True,
                "SUPPORT_MODE": "bot",
                "SUPPORT_BOT_USERNAME": "@cw_help_bot",
            },
        )
        await uow.commit()
    landing = (await http.get("/api/cabinet/public/landing")).json()
    assert landing["support_web"] is True
    assert landing["support_telegram"] == "cw_help_bot"
    assert (await http.get("/api/cabinet/config")).json()["support_telegram"] == "cw_help_bot"


async def test_leftover_support_account_of_another_mode_is_not_public(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """A personal redirect account left over from an old mode never reaches the public API."""
    http, container = client
    async with container.uow() as uow:
        await container.bot_config.set_values(
            uow,
            {
                "SUPPORT_MODE": "tickets",
                "SUPPORT_REDIRECT_USERNAME": "owner_personal",
                "SUPPORT_BOT_USERNAME": "old_help_bot",
            },
        )
        await uow.commit()
    assert (await http.get("/api/cabinet/public/landing")).json()["support_telegram"] == ""
    assert (await http.get("/api/cabinet/config")).json()["support_telegram"] == ""
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, {"SUPPORT_MODE": "redirect"})
        await uow.commit()
    assert (await http.get("/api/cabinet/public/landing")).json()[
        "support_telegram"
    ] == "owner_personal"
