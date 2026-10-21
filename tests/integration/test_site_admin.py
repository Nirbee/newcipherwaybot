"""Admin «Сайт»: links, cabinet + SMTP settings, site copy, a real test e-mail."""

from __future__ import annotations

import httpx

from tests.integration.test_admin_api import ApiTestContainer, _login, client  # noqa: F401


async def test_site_screen_reads_and_saves_settings_and_copy(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    auth = await _login(http)
    state = (await http.get("/api/admin/site", headers=auth)).json()
    assert state["settings"]["SMTP_PASSWORD"] == {"set": False}  # secrets never sent back
    assert "landing" in state and "template" in state

    res = await http.patch(
        "/api/admin/site",
        headers=auth,
        json={
            "settings": {
                "WEB_CABINET_ENABLED": True,
                "SMTP_HOST": "smtp.yandex.ru",
                "SMTP_PORT": 465,
                "SMTP_FROM": "noreply@example.com",
                "SMTP_PASSWORD": "app-pass",
                "NOT_A_SITE_KEY": "ignored",
            },
            "landing": {"headline": "VPN без границ", "faq": [{"q": "Как?", "a": "Так"}]},
        },
    )
    assert res.status_code == 200, res.text
    saved = res.json()
    assert saved["settings"]["WEB_CABINET_ENABLED"] is True
    assert saved["settings"]["SMTP_PASSWORD"] == {"set": True}
    assert saved["mail_ready"] is True
    assert saved["landing"]["headline"] == "VPN без границ"
    assert saved["landing"]["faq"][0]["q"] == "Как?"

    # an empty secret field keeps the stored password
    await http.patch("/api/admin/site", headers=auth, json={"settings": {"SMTP_PASSWORD": ""}})
    async with container.uow() as uow:
        assert await container.bot_config.value(uow, "SMTP_PASSWORD") == "app-pass"

    # the site copy is the same data the public site reads
    landing = (await http.get("/api/cabinet/public/landing")).json()
    assert "VPN без границ" in str(landing)


async def test_test_email_reports_the_real_smtp_error(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch
) -> None:
    http, container = client
    auth = await _login(http)
    res = await http.post("/api/admin/site/test-email", headers=auth, json={"to": "a@b.ru"})
    assert res.status_code == 400  # SMTP not configured yet

    await http.patch(
        "/api/admin/site",
        headers=auth,
        json={"settings": {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "x@example.com"}},
    )
    from src.infrastructure.services import mailer as mailer_mod

    async def boom(self, to: str, subject: str, body: str) -> None:
        raise mailer_mod.smtplib.SMTPAuthenticationError(535, b"bad password")

    monkeypatch.setattr(mailer_mod.Mailer, "send_strict", boom)
    res = (
        await http.post("/api/admin/site/test-email", headers=auth, json={"to": "a@b.ru"})
    ).json()
    assert res["ok"] is False and "SMTPAuthenticationError" in res["error"]

    sent: list[str] = []

    async def ok(self, to: str, subject: str, body: str) -> None:
        sent.append(to)

    monkeypatch.setattr(mailer_mod.Mailer, "send_strict", ok)
    res = (
        await http.post("/api/admin/site/test-email", headers=auth, json={"to": "a@b.ru"})
    ).json()
    assert res == {"ok": True} and sent == ["a@b.ru"]


async def _customer_with_sub(container: ApiTestContainer, *, username: str, telegram_id: int):
    from src.application.dto.pricing import PurchaseRequest
    from src.core.enums import Currency
    from tests.factories import make_plan, make_user

    async with container.uow() as uow:
        user = await make_user(uow, telegram_id=telegram_id)
        user.username = username
        plan, _ = await make_plan(uow, code=f"renew-{telegram_id}", name="Стандартный")
        await uow.commit()
        req = PurchaseRequest(
            user_id=user.id, plan_id=plan.id, duration_days=30, currency=Currency.RUB
        )
        sub = await container.subscriptions.grant(uow, user=user, plan=plan, req=req)
        user.current_subscription_id = sub.id
        await uow.commit()
        return user.id, plan.id


async def test_renew_by_telegram_username_without_login(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch
) -> None:
    """A customer whose VPN ran out can't open Telegram: on the site they type their @username
    and pay; the SAME subscription is renewed. Only a masked handle and the plan are shown."""
    from src.web.routes import cabinet as cabinet_routes

    http, container = client
    user_id, plan_id = await _customer_with_sub(
        container, username="kxmirrr", telegram_id=5072755730
    )
    auth = await _login(http)

    # off until the web cabinet is enabled
    assert (
        await http.post("/api/cabinet/public/renew/lookup", json={"who": "@kxmirrr"})
    ).status_code == 403
    await http.patch(
        "/api/admin/site", headers=auth, json={"settings": {"WEB_CABINET_ENABLED": True}}
    )

    for who in ("@KxMirrr", "https://t.me/kxmirrr", "5072755730"):
        res = await http.post("/api/cabinet/public/renew/lookup", json={"who": who})
        assert res.status_code == 200, (who, res.text)
    found = res.json()
    assert found["who"] == "@kx•••rr" and "kxmirrr" not in str(found)  # masked, nothing else leaked
    assert found["active"] is True and found["plan_name"] == "Стандартный"
    assert found["items"][0]["id"] == plan_id  # their plan first
    assert "subscription_url" not in str(found)

    missing = await http.post("/api/cabinet/public/renew/lookup", json={"who": "@nobody_here"})
    assert missing.status_code == 404

    captured: dict = {}

    async def fake_pay(container_, user, req, method, *, return_url=None):
        captured.update(
            user=user.id,
            plan=req.plan_id,
            days=req.duration_days,
            method=method,
            ptype=req.purchase_type,
            return_url=return_url,
        )
        return {"ok": True, "redirect_url": "https://pay.example/123"}

    monkeypatch.setattr(cabinet_routes, "_pay_with_gateway", fake_pay)
    bad = await http.post(
        "/api/cabinet/public/renew/pay",
        json={"token": "x" * 20, "plan_id": plan_id, "days": 30, "method": "yookassa"},
    )
    assert bad.status_code == 401
    stars = await http.post(
        "/api/cabinet/public/renew/pay",
        json={"token": found["token"], "plan_id": plan_id, "days": 30, "method": "stars"},
    )
    assert stars.status_code == 400
    paid = await http.post(
        "/api/cabinet/public/renew/pay",
        json={"token": found["token"], "plan_id": plan_id, "days": 30, "method": "yookassa"},
    )
    assert paid.json()["redirect_url"] == "https://pay.example/123"
    assert captured["user"] == user_id and captured["ptype"].name == "RENEW"
    assert (captured["return_url"] or "").endswith("/web/?renewed=1") or captured[
        "return_url"
    ] is None
