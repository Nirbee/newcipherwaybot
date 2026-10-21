"""Admin: «Сайт» — the public sales site (served at /) and the web cabinet (/web) in one place.

The site is for people who can't open Telegram/the bot (no VPN yet): it shows the tariffs and
sends them to the web cabinet, where they register by e-mail (or Google/Yandex/VK) and buy.
This screen gathers what used to be scattered: the site's copy (stored in the mini-app config's
``ui.landing``, the site shares the mini-app's theme), the cabinet switches and the SMTP mailer,
plus the two links to hand out.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.application.services.bot_config import BotConfigError
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import audit
from src.web.routes.admin.deps import AdminIdentity, require_admin
from src.web.routes.cabinet_auth import _redirect_uri

router = APIRouter(prefix="/site")

_KEYS = (
    "WEB_CABINET_ENABLED",
    "CABINET_URL",
    "CABINET_EMAIL_VERIFICATION",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "OAUTH_GOOGLE_CLIENT_ID",
    "OAUTH_GOOGLE_CLIENT_SECRET",
    "OAUTH_YANDEX_CLIENT_ID",
    "OAUTH_YANDEX_CLIENT_SECRET",
    "OAUTH_VK_CLIENT_ID",
    "OAUTH_VK_CLIENT_SECRET",
)
# Never sent back to the browser; the form shows «задан» and an empty field keeps the value.
_SECRETS = {
    "SMTP_PASSWORD",
    "OAUTH_GOOGLE_CLIENT_SECRET",
    "OAUTH_YANDEX_CLIENT_SECRET",
    "OAUTH_VK_CLIENT_SECRET",
}


def _base_url(container: AppContainer) -> str:
    return (container.settings.web.public_url or "").strip().rstrip("/")


async def _site_state(container: AppContainer) -> dict[str, Any]:
    async with container.uow() as uow:
        values = {k: await container.bot_config.value(uow, k) for k in _KEYS}
        cfg = await uow.miniapp.get_or_create()
        await uow.commit()
        template, landing = cfg.template, dict((cfg.ui or {}).get("landing") or {})
    base = _base_url(container)
    cabinet = str(values["CABINET_URL"] or "").strip() or (f"{base}/web/" if base else "")
    settings: dict[str, Any] = {}
    for key, value in values.items():
        settings[key] = {"set": bool(value)} if key in _SECRETS else value
    return {
        "site_url": f"{base}/" if base else "",
        "cabinet_url": cabinet,
        # exactly what the cabinet sends to Google/Yandex/VK — paste it into their consoles
        "oauth_redirect": _redirect_uri(cabinet) if cabinet else "",
        "template": template,
        "landing": landing,
        "settings": settings,
        "mail_ready": bool(values["SMTP_HOST"] and values["SMTP_FROM"]),
    }


@router.get("")
async def get_site(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    return await _site_state(container)


class SitePatch(BaseModel):
    settings: dict[str, Any] | None = None
    landing: dict[str, Any] | None = None


@router.patch("")
async def patch_site(
    body: SitePatch,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    changes = {
        k: v
        for k, v in (body.settings or {}).items()
        # an empty secret field means «keep what's stored»
        if k in _KEYS and not (k in _SECRETS and (v is None or v == ""))
    }
    async with container.uow() as uow:
        if changes:
            try:
                await container.bot_config.set_values(uow, changes)
            except BotConfigError as exc:
                raise HTTPException(400, str(exc)) from exc
        if body.landing is not None:
            # Same sanitiser the mini-app editor goes through (the site's copy lives there).
            from src.web.routes.admin.miniapp import MiniappPatch

            cfg = await uow.miniapp.get_or_create()
            ui = MiniappPatch(ui={**(cfg.ui or {}), "landing": body.landing}).ui or {}
            cfg.ui = ui
        await audit(
            uow,
            identity,
            "site.patch",
            None,
            keys=sorted(changes),
            landing=body.landing is not None,
        )
        await uow.commit()
    return await _site_state(container)


class TestMailIn(BaseModel):
    to: str = Field(..., min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/test-email")
async def test_email(
    body: TestMailIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Send a real test letter and report the SMTP error verbatim if it fails."""
    mailer = await container.build_mailer()
    if not mailer.configured:
        raise HTTPException(400, "не заполнены SMTP-сервер и адрес отправителя")
    try:
        await mailer.send_strict(
            body.to,
            "Проверка почты CipherWay",
            "Это тестовое письмо из админ-панели: отправка писем настроена правильно.",
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    async with container.uow() as uow:
        await audit(uow, identity, "site.test_email", None, to=body.to)
        await uow.commit()
    return {"ok": True}
