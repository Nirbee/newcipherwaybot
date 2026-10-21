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
