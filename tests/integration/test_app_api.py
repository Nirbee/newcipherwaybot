"""Windows-app API (/api/app/v1): auth by the subscription-link token, limits, support chat.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.application.dto.panel import PanelUser
from src.core.enums import (
    SubscriptionStatus,
    TicketAuthor,
    TicketChannel,
    TicketStatus,
    UserStatus,
)
from src.core.exceptions import RemnawaveTransientError
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.ticket import Ticket, TicketMessage
from src.infrastructure.services.ai_support import AiSupportService
from src.infrastructure.services.notification import LogNotifier
from src.web.routes.panel import _apply_user_event
from tests.factories import make_user
from tests.integration.test_admin_api import ApiTestContainer, _login, _tma_headers, client
from tests.integration.test_support_and_user_tools import PNG

_FIXTURES = (client,)  # the shared ASGI-client fixture, re-exported (keeps ruff F401/F811 quiet)


class _FakeAi:
    """Stands in for AiSupportService: answers every ticket with a 🤖 reply like the real one
    and records which subscription the call was pinned to."""

    def __init__(self, container: ApiTestContainer) -> None:
        self._container = container
        self.calls: list[dict[str, Any]] = []

    async def handle_ticket(
        self, user: Any, ticket_id: int, **kwargs: Any
    ) -> tuple[str, str | None]:
        self.calls.append(kwargs)
        async with self._container.uow() as uow:
            await uow.ticket_messages.add(
                TicketMessage(ticket_id=ticket_id, author=TicketAuthor.ADMIN, text="🤖 Привет!")
            )
            await uow.commit()
        return "reply", "Привет!"


def _h(token: str) -> dict[str, str]:
    return {"X-Subscription-Token": token}


def _panel_user(panel_uuid: uuid.UUID, token: str, short_id: str) -> PanelUser:
    return PanelUser(
        uuid=panel_uuid,
        short_id=token,
        username=f"sub_{short_id}",
        is_enabled=True,
        expire_at=None,
        traffic_limit_bytes=0,
        traffic_used_bytes=0,
        device_limit=None,
        subscription_url=f"https://panel.test/sub/{token}",
    )


async def _sub(
    container: ApiTestContainer,
    token: str,
    *,
    tg: int | None = 5500001,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    **user_fields: Any,
) -> tuple[int, int]:
    """A customer whose panel user's link token is ``token`` (bot-style: our short_id is
    only the username suffix). Returns (user_id, subscription_id)."""
    panel_uuid = uuid.uuid4()
    short_id = uuid.uuid4().hex[:10]
    async with container.uow() as uow:
        user = await make_user(uow, telegram_id=tg, **user_fields)  # type: ignore[arg-type]
        sub = Subscription(
            user_id=user.id,
            short_id=short_id,
            remnawave_uuid=panel_uuid,
            status=status,
            subscription_url=f"https://panel.test/sub/{token}",
        )
        await uow.subscriptions.add(sub)
        user.current_subscription_id = sub.id
        await uow.commit()
        ids = (user.id, sub.id)
    container.remnawave_client.users[panel_uuid] = _panel_user(panel_uuid, token, short_id)
    return ids


async def test_token_resolves_through_the_panel_and_is_cached(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "PanelTok9876543", username="vasya", email="v@example.com")
    for _ in range(3):
        res = await http.get("/api/app/v1/me", headers=_h("PanelTok9876543"))
        assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "has_telegram": True}  # no nickname / e-mail
    assert container.remnawave_client.short_uuid_lookups == 1  # then served from Redis


async def test_me_without_telegram(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "WebTok12345678", tg=None, username="web-login")
    res = await http.get("/api/app/v1/me", headers=_h("WebTok12345678"))
    assert res.json() == {"ok": True, "has_telegram": False}


async def test_local_short_id_is_not_a_credential(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """Even an imported sub (short_id == its link token) is confirmed by the panel: a link
    revoked on the panel is dead even though our row still carries the old token."""
    http, container = client
    async with container.uow() as uow:
        user = await make_user(uow, telegram_id=5500002)
        await uow.subscriptions.add(
            Subscription(
                user_id=user.id,
                short_id="ImpTok12345678",
                subscription_url="https://panel.test/sub/ImpTok12345678",
            )
        )
        await uow.commit()
    assert (await http.get("/api/app/v1/me", headers=_h("ImpTok12345678"))).status_code == 401
    assert container.remnawave_client.short_uuid_lookups == 1


async def test_bad_tokens_get_one_identical_401_and_are_remembered(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "RealTok1234567")
    answers = [
        await http.get("/api/app/v1/support"),  # no header
        await http.get("/api/app/v1/support", headers=_h("x")),  # too short
        await http.get("/api/app/v1/support", headers=_h("../../etc/passwd")),  # bad alphabet
        await http.get("/api/app/v1/support", headers=_h("UnknownTok12345")),  # well-formed
        await http.get("/api/app/v1/support", headers=_h("UnknownTok12345")),  # again
    ]
    assert {r.status_code for r in answers} == {401}
    assert len({r.text for r in answers}) == 1
    # only the first well-formed unknown token reached the panel; the repeat hit app:bad
    assert container.remnawave_client.short_uuid_lookups == 1


async def test_panel_must_echo_the_exact_token(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "ExactTok123456")

    async def sloppy(short_uuid: str) -> PanelUser | None:  # e.g. case-insensitive match
        return next(iter(container.remnawave_client.users.values()))

    container.remnawave_client.get_user_by_short_uuid = sloppy  # type: ignore[method-assign]
    assert (await http.get("/api/app/v1/me", headers=_h("exacttok123456"))).status_code == 401


async def test_reset_link_kills_the_old_token_at_once(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "OldTok12345678", tg=5500003)
    assert (await http.get("/api/app/v1/me", headers=_h("OldTok12345678"))).status_code == 200
    res = await http.post("/api/cabinet/subscription/reset-link", headers=_tma_headers(5500003))
    assert res.status_code == 200, res.text
    assert (await http.get("/api/app/v1/me", headers=_h("OldTok12345678"))).status_code == 401
    new_token = res.json()["subscription_url"].rsplit("/", 1)[-1]
    assert (await http.get("/api/app/v1/me", headers=_h(new_token))).status_code == 200


async def test_panel_side_revoke_via_webhook_kills_the_cached_token(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """Revoked in the panel UI / MultiScript: the webhook syncs the new link onto our row, the
    cache no longer matches it, the panel no longer knows the old token → 401."""
    http, container = client
    _, sub_id = await _sub(container, "PnlTok12345678")
    assert (await http.get("/api/app/v1/me", headers=_h("PnlTok12345678"))).status_code == 200
    fake = container.remnawave_client
    panel_uuid = next(iter(fake.users))
    rotated = await fake.revoke_subscription(panel_uuid)
    applied = await _apply_user_event(
        container,  # type: ignore[arg-type]
        "user.modified",
        {
            "uuid": str(panel_uuid),
            "shortUuid": rotated.short_id,
            "username": rotated.username,
            "status": "ACTIVE",
            "subscriptionUrl": rotated.subscription_url,
        },
    )
    assert applied
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(sub_id)
        assert sub is not None and sub.subscription_url == rotated.subscription_url
    assert (await http.get("/api/app/v1/me", headers=_h("PnlTok12345678"))).status_code == 401


async def test_rate_limits(client: tuple[httpx.AsyncClient, ApiTestContainer]) -> None:
    http, container = client
    await _sub(container, "LimTok12345678")
    codes = [
        (await http.get("/api/app/v1/support", headers=_h("LimTok12345678"))).status_code
        for _ in range(31)
    ]
    assert codes[:30] == [200] * 30 and codes[30] == 429

    # Guessing links: after 20 failed resolves the IP gets 429 without touching the panel.
    container.redis.store.clear()
    lookups = container.remnawave_client.short_uuid_lookups
    codes = [
        (await http.get("/api/app/v1/me", headers=_h(f"Guess{i:010d}"))).status_code
        for i in range(25)
    ]
    assert codes[:20] == [401] * 20 and set(codes[20:]) == {429}
    assert container.remnawave_client.short_uuid_lookups - lookups == 20


async def test_ipv6_clients_are_limited_per_64(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "SixTok12345678")
    for i in range(5):  # a fresh address of the same /64 every request
        h = _h("SixTok12345678") | {"X-Real-IP": f"2001:db8:1:2::{i + 1:x}"}
        assert (await http.get("/api/app/v1/me", headers=h)).status_code == 200
    # all five landed in ONE per-IP counter — rotating within the /64 doesn't reset it
    assert container.redis.store["rl:auth:app-get:2001:db8:1:2::/64"] == "5"


async def test_global_panel_resolve_ceiling_is_503(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.redis.store["rl:app:panel-resolve"] = "300"
    res = await http.get("/api/app/v1/me", headers=_h("AnyTok12345678"))
    assert res.status_code == 503
    assert container.remnawave_client.short_uuid_lookups == 0


async def test_panel_outage_is_503_not_401(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client

    async def down(short_uuid: str) -> PanelUser | None:
        raise RemnawaveTransientError("panel down")

    container.remnawave_client.get_user_by_short_uuid = down  # type: ignore[method-assign]
    assert (await http.get("/api/app/v1/me", headers=_h("SomeTok12345678"))).status_code == 503
    assert "app:bad:" not in "".join(container.redis.store)  # an outage isn't «bad token»


async def test_blocked_customer_is_refused(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    user_id, _ = await _sub(container, "BlkTok12345678")
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        assert user is not None
        user.status = UserStatus.BLOCKED
        await uow.commit()
    assert (await http.get("/api/app/v1/me", headers=_h("BlkTok12345678"))).status_code == 403


async def test_expired_customer_writes_to_support_and_admin_sees_the_source(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    ai = _FakeAi(container)
    container.ai_support = ai  # type: ignore[attr-defined]
    _, sub_id = await _sub(container, "ExpTok12345678", status=SubscriptionStatus.EXPIRED)
    h = _h("ExpTok12345678")
    assert (await http.get("/api/app/v1/support", headers=h)).json() == {
        "ticket_id": None,
        "status": None,
        "messages": [],
    }
    res = await http.post("/api/app/v1/support", headers=h, json={"text": "Не подключается"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["ai_outcome"] == "reply" and body["ai_reply"] == "Привет!"
    assert ai.calls == [{"subscription_id": sub_id}]  # the AI is pinned to the token's sub

    history = (await http.get("/api/app/v1/support", headers=h)).json()
    assert history["ticket_id"] == body["ticket_id"]
    assert [(m["from"], m["text"]) for m in history["messages"]] == [
        ("you", "Не подключается"),
        ("support", "Привет!"),
    ]

    auth = await _login(http)
    items = (await http.get("/api/admin/tickets", headers=auth)).json()["items"]
    mine = next(i for i in items if i["id"] == body["ticket_id"])
    assert mine["subject"] == "💻 Не подключается"


async def test_app_does_not_see_or_continue_bot_tickets(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.ai_support = _FakeAi(container)  # type: ignore[attr-defined]
    user_id, _ = await _sub(container, "PrvTok12345678")
    async with container.uow() as uow:
        bot_ticket = Ticket(user_id=user_id, subject="личное из бота", status=TicketStatus.OPEN)
        await uow.tickets.add(bot_ticket)
        await uow.ticket_messages.add(
            TicketMessage(ticket_id=bot_ticket.id, author=TicketAuthor.USER, text="секрет")
        )
        await uow.commit()
        bot_ticket_id = bot_ticket.id
    h = _h("PrvTok12345678")
    assert (await http.get("/api/app/v1/support", headers=h)).json()["ticket_id"] is None
    res = await http.post("/api/app/v1/support", headers=h, json={"text": "вопрос"})
    app_ticket_id = res.json()["ticket_id"]
    assert app_ticket_id != bot_ticket_id
    history = (await http.get("/api/app/v1/support", headers=h)).json()
    assert "секрет" not in [m["text"] for m in history["messages"]]
    async with container.uow() as uow:
        rows = await uow.ticket_messages.list(ticket_id=bot_ticket_id)
        assert [m.text for m in rows] == ["секрет"]  # nothing appended to the bot thread


async def test_daily_message_quota(client: tuple[httpx.AsyncClient, ApiTestContainer]) -> None:
    http, container = client
    container.ai_support = _FakeAi(container)  # type: ignore[attr-defined]
    user_id, _ = await _sub(container, "DayTok12345678")
    container.redis.store[f"rl:auth:app-day-post:{user_id}"] = "200"
    res = await http.post("/api/app/v1/support", headers=_h("DayTok12345678"), json={"text": "x"})
    assert res.status_code == 429


async def test_text_body_over_16kb_is_413(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "BigTok12345678")
    res = await http.post(
        "/api/app/v1/support",
        headers=_h("BigTok12345678") | {"content-type": "application/json"},
        content=b'{"text": "' + b"a" * 20000 + b'"}',
    )
    assert res.status_code == 413


async def test_photo_needs_content_length_and_a_cap(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "LenTok12345678")
    h = _h("LenTok12345678")

    async def chunks():  # type: ignore[no-untyped-def]
        yield b"--x\r\n"

    chunked = await http.post(
        "/api/app/v1/support/photo",
        headers=h | {"content-type": "multipart/form-data; boundary=x"},
        content=chunks(),
    )
    assert chunked.status_code == 411
    huge = await http.post(
        "/api/app/v1/support/photo",
        headers=h | {"content-type": "multipart/form-data; boundary=x"},
        content=b"0" * (10 * 1024 * 1024 + 70 * 1024),
    )
    assert huge.status_code == 413
    # no token → 401 before the body is even looked at
    anon = await http.post(
        "/api/app/v1/support/photo",
        files={"file": ("screen.png", PNG, "image/png")},
    )
    assert anon.status_code == 401


async def test_app_customer_can_send_a_screenshot(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _sub(container, "PicTok12345678")
    h = _h("PicTok12345678")
    res = await http.post(
        "/api/app/v1/support/photo",
        headers=h,
        # the declared type is ignored — the PNG signature decides (Electron may send octet-stream)
        files={"file": ("screen.bin", PNG, "application/octet-stream")},
        data={"text": "вот ошибка"},
    )
    assert res.status_code == 200, res.text
    history = (await http.get("/api/app/v1/support", headers=h)).json()
    image = history["messages"][-1]["image"]
    try:
        assert image.startswith("/uploads/tickets/") and image.endswith(".png")
        assert history["messages"][-1]["text"] == "вот ошибка"
        async with container.uow() as uow:
            ticket = await uow.tickets.get(res.json()["ticket_id"])
            assert ticket is not None and ticket.subject == "💻 вот ошибка"
        served = await http.get(image)
        assert served.headers["x-content-type-options"] == "nosniff"
        # HTML dressed up as a PNG is refused by its signature
        fake = await http.post(
            "/api/app/v1/support/photo",
            headers=h,
            files={"file": ("x.png", b"<html><script>alert(1)</script>", "image/png")},
        )
        assert fake.status_code == 400
        two = await http.post(
            "/api/app/v1/support/photo",
            headers=h,
            files=[("file", ("a.png", PNG, "image/png")), ("file", ("b.png", PNG, "image/png"))],
        )
        assert two.status_code == 400
    finally:
        Path(image.lstrip("/")).unlink(missing_ok=True)


async def test_ai_in_the_app_channel_never_hands_out_the_link(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    _, container = client
    user_id, sub_id = await _sub(container, "AiTok123456789")
    async with container.uow() as uow:
        other = await make_user(uow, telegram_id=5500999)
        foreign = Subscription(user_id=other.id, short_id="foreign001")
        await uow.subscriptions.add(foreign)
        await uow.commit()
        foreign_id = foreign.id
        user = await uow.users.get(user_id)
    assert user is not None
    ai = AiSupportService(
        container.remnawave_client,  # type: ignore[arg-type]
        LogNotifier(),
        container.bot_config,
        container.uow,
    )
    # bot / cabinet channel: the link is available to the model (owner policy permitting)
    text, _ = await ai._tool("check_subscription", user, {})
    assert "AiTok123456789" in text
    # app channel: never — whoever holds an old token must not be handed the current link
    text, _ = await ai._tool("check_subscription", user, {}, subscription_id=sub_id)
    assert "AiTok123456789" not in text and "panel.test" not in text
    text, _ = await ai._tool("check_payment", user, {}, subscription_id=sub_id)
    assert "ESCALATE" in text
    assert "Windows" in await ai._context(user, sub_id)
    # a subscription id that isn't this customer's is ignored, not acted on
    text, _ = await ai._tool("reset_devices", user, {}, subscription_id=foreign_id)
    assert "нет активной подписки" in text


async def _add_sub(container: ApiTestContainer, user_id: int, token: str) -> int:
    """A second subscription of the same account, with its own link token."""
    panel_uuid = uuid.uuid4()
    short_id = uuid.uuid4().hex[:10]
    async with container.uow() as uow:
        sub = Subscription(
            user_id=user_id,
            short_id=short_id,
            remnawave_uuid=panel_uuid,
            status=SubscriptionStatus.ACTIVE,
            subscription_url=f"https://panel.test/sub/{token}",
        )
        await uow.subscriptions.add(sub)
        await uow.commit()
        sub_id = sub.id
    container.remnawave_client.users[panel_uuid] = _panel_user(panel_uuid, token, short_id)
    return sub_id


async def test_two_subscriptions_of_one_account_get_separate_threads(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.ai_support = _FakeAi(container)  # type: ignore[attr-defined]
    user_id, sub_a = await _sub(container, "SubATok1234567")
    sub_b = await _add_sub(container, user_id, "SubBTok1234567")
    a, b = _h("SubATok1234567"), _h("SubBTok1234567")
    ta = (await http.post("/api/app/v1/support", headers=a, json={"text": "от A"})).json()
    tb = (await http.post("/api/app/v1/support", headers=b, json={"text": "от B"})).json()
    assert ta["ticket_id"] != tb["ticket_id"]
    hist_b = (await http.get("/api/app/v1/support", headers=b)).json()
    assert [m["text"] for m in hist_b["messages"] if m["from"] == "you"] == ["от B"]
    async with container.uow() as uow:
        t_a = await uow.tickets.get(ta["ticket_id"])
        t_b = await uow.tickets.get(tb["ticket_id"])
        assert t_a is not None and t_b is not None
        assert (t_a.channel, t_a.subscription_id) == (TicketChannel.APP, sub_a)
        assert (t_b.channel, t_b.subscription_id) == (TicketChannel.APP, sub_b)


async def test_bot_message_never_lands_in_an_app_ticket_and_vice_versa(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    from src.application.services import premium
    from src.bot.handlers.tickets import _append_ticket_message

    http, container = client
    container.ai_support = _FakeAi(container)  # type: ignore[attr-defined]
    user_id, _ = await _sub(container, "MixTok12345678", tg=5500777)
    app_ticket = (
        await http.post("/api/app/v1/support", headers=_h("MixTok12345678"), json={"text": "a"})
    ).json()["ticket_id"]

    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        assert user is not None
        # the open app ticket is invisible to the bot / premium flows
        assert await premium.active_ticket(uow, user_id, premium_only=False) is None
    bot_ticket, created, _ = await _append_ticket_message(container, user, text="из бота")  # type: ignore[arg-type]
    assert created and bot_ticket != app_ticket
    # the mini-app / cabinet continues the BOT thread, not the app one
    cab = await http.get("/api/cabinet/support", headers=_tma_headers(5500777))
    assert cab.json()["ticket_id"] == bot_ticket
    # ... and the app keeps its own thread
    hist = (await http.get("/api/app/v1/support", headers=_h("MixTok12345678"))).json()
    assert hist["ticket_id"] == app_ticket
    assert "из бота" not in [m["text"] for m in hist["messages"]]
    async with container.uow() as uow:
        rows = await uow.ticket_messages.list(ticket_id=app_ticket)
        assert "из бота" not in [m.text for m in rows]


async def test_admin_queue_shows_the_channel(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.ai_support = _FakeAi(container)  # type: ignore[attr-defined]
    _, sub_id = await _sub(container, "AdmTok12345678")
    tid = (
        await http.post("/api/app/v1/support", headers=_h("AdmTok12345678"), json={"text": "q"})
    ).json()["ticket_id"]
    auth = await _login(http)
    row = next(
        i
        for i in (await http.get("/api/admin/tickets", headers=auth)).json()["items"]
        if i["id"] == tid
    )
    assert row["channel"] == "app"
    detail = (await http.get(f"/api/admin/tickets/{tid}", headers=auth)).json()
    assert detail["channel"] == "app" and detail["subscription_id"] == sub_id


async def test_cabinet_photo_is_parsed_after_auth_and_rate_limited(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    # no initData → 401, the body is never parsed
    anon = await http.post(
        "/api/cabinet/support/photo", files={"file": ("s.png", PNG, "image/png")}
    )
    assert anon.status_code == 401
    tma = _tma_headers(tg_id=5500888)

    async def chunks():  # type: ignore[no-untyped-def]
        yield b"--x\r\n"

    chunked = await http.post(
        "/api/cabinet/support/photo",
        headers=tma | {"content-type": "multipart/form-data; boundary=x"},
        content=chunks(),
    )
    assert chunked.status_code == 411
    container.redis.store.clear()
    images: list[str] = []
    try:
        codes = []
        for _ in range(6):
            res = await http.post(
                "/api/cabinet/support/photo",
                headers=tma,
                files={"file": ("s.png", PNG, "image/png")},
            )
            codes.append(res.status_code)
        assert codes[:5] == [200] * 5 and codes[5] == 429  # 5/min per customer
        history = (await http.get("/api/cabinet/support", headers=tma)).json()
        images = [m["image"] for m in history["messages"] if m["image"]]
        async with container.uow() as uow:
            t = await uow.tickets.get(history["ticket_id"])
            assert t is not None and t.channel is TicketChannel.CABINET
    finally:
        for image in images:
            Path(image.lstrip("/")).unlink(missing_ok=True)


async def test_operator_reply_to_an_app_ticket_is_not_sent_to_telegram(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link holder may not be the account owner: an app thread is answered only in the
    app (it polls /support), never DM'd — while a bot thread still is."""
    import aiogram

    from tests.integration.test_premium import _Recorder

    sent: list[int] = []

    class _Bot:
        def __init__(self, token: str) -> None:
            self.session = self

        async def send_message(self, chat_id: int, text: str) -> None:
            sent.append(chat_id)

        async def send_photo(self, chat_id: int, *a: Any, **k: Any) -> None:
            sent.append(chat_id)

        async def close(self) -> None: ...

    monkeypatch.setattr(aiogram, "Bot", _Bot)
    http, container = client
    container.ai_support = _FakeAi(container)  # type: ignore[attr-defined]
    rec = _Recorder()
    container.notifier = rec  # type: ignore[assignment]
    user_id, _ = await _sub(container, "TgTok123456789", tg=5501234)
    app_ticket = (
        await http.post("/api/app/v1/support", headers=_h("TgTok123456789"), json={"text": "?"})
    ).json()["ticket_id"]
    auth = await _login(http)

    res = await http.post(
        f"/api/admin/tickets/{app_ticket}/reply", headers=auth, json={"text": "Ответ"}
    )
    assert res.status_code == 200, res.text
    assert sent == []  # nothing went to Telegram
    hist = (await http.get("/api/app/v1/support", headers=_h("TgTok123456789"))).json()
    assert hist["messages"][-1] == {**hist["messages"][-1], "from": "support", "text": "Ответ"}

    offer = await http.post(
        f"/api/admin/tickets/{app_ticket}/premium-offer",
        headers=auth,
        json={"name": "Премиум", "durations": [{"days": 30, "price_minor": 100000}]},
    )
    assert offer.status_code == 200, offer.text
    assert rec.users == []  # the offer is in the app thread only

    # control: a bot thread of the same customer is still delivered to Telegram
    async with container.uow() as uow:
        bot_ticket = Ticket(user_id=user_id, subject="бот", status=TicketStatus.OPEN)
        await uow.tickets.add(bot_ticket)
        await uow.commit()
        bot_ticket_id = bot_ticket.id
    await http.post(f"/api/admin/tickets/{bot_ticket_id}/reply", headers=auth, json={"text": "ок"})
    assert sent == [5501234]
