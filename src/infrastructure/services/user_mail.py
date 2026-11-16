"""Letters to people without Telegram (web cabinet / guest buyers): payment, expiry, reset.

The owner edits subject + plain text in the admin («Уведомления» → «Письма», see
``email_template``); this module fills the shared values (cabinet link, support contact,
Windows app), and sends the text together with an HTML alternative built from it. The HTML
escapes EVERYTHING — the owner's text and every value — and only then turns bare http(s)
links into anchors, so a plan or user name can never inject markup into a letter.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

from src.core.logging import get_logger

if TYPE_CHECKING:
    from src.infrastructure.database.models.user import User
    from src.infrastructure.database.uow import UnitOfWork
    from src.infrastructure.di import AppContainer

log = get_logger(__name__)

_DEFAULT_BRAND = "CipherWay"
# Bare links inside already-escaped text. Stops at whitespace and at '<'; trailing
# punctuation of the sentence is not part of the link.
_LINK = re.compile(r"https?://[^\s<]+")
_TRAILING = ".,:!?)»"  # not ";": it ends an escaped entity


async def cabinet_base_url(container: AppContainer, uow: UnitOfWork) -> str:
    """The web cabinet page: CABINET_URL, else ``<public url>/web/``; "" when neither is set.

    A bare-domain CABINET_URL (older installs autofilled it so) gets ``/web/`` — on ``/`` lives
    the landing, which knows nothing about #reset / #login links (same rule as _redirect_uri)."""
    from urllib.parse import urlparse

    url = str(await container.bot_config.value(uow, "CABINET_URL") or "").strip()
    if not url:
        base = (container.settings.web.public_url or "").strip().rstrip("/")
        url = f"{base}/web/" if base else ""
    if url and urlparse(url).path in ("", "/"):
        url = f"{url.rstrip('/')}/web/"
    if url and not url.endswith("/"):
        url += "/"
    return url


async def support_username(container: AppContainer, uow: UnitOfWork) -> str:
    """Telegram username of support for the CURRENT support mode only, without «@»: the support
    bot in «bot» mode, the redirect account in «redirect» mode, "" otherwise. A leftover value
    from a mode the owner switched away from (e.g. a personal account) is never exposed — this
    feeds the public /config and the landing page."""
    cfg = container.bot_config
    mode = str(await cfg.value(uow, "SUPPORT_MODE") or "")
    bot = str(await cfg.value(uow, "SUPPORT_BOT_USERNAME") or "").strip().lstrip("@")
    person = str(await cfg.value(uow, "SUPPORT_REDIRECT_USERNAME") or "").strip().lstrip("@")
    if mode == "bot":
        return bot
    if mode == "redirect":
        return person
    return ""


async def support_contact(container: AppContainer, uow: UnitOfWork) -> str:
    """«Поддержка: https://t.me/…» from the support settings, or "" when there is none."""
    name = await support_username(container, uow)
    return f"Поддержка: https://t.me/{name}" if name else ""


async def brand_title(uow: UnitOfWork) -> str:
    row = await uow.miniapp.find_one()  # read only: never create the row from a worker
    return str(getattr(row, "title", "") or "").strip() or _DEFAULT_BRAND


async def windows_app_text(container: AppContainer, uow: UnitOfWork, brand: str) -> str:
    url = str(await container.bot_config.value(uow, "WINDOWS_APP_URL") or "").strip()
    return f"наше приложение {brand}: {url}" if url else "Happ или Hiddify"


def cabinet_login_link(container: AppContainer, user: User, cabinet: str) -> str:
    """Where «Войти в кабинет» in a letter leads.

    A one-click link (fresh 72 h auto-login token in the URL fragment) ONLY for an account the
    guest purchase created whose owner has never chosen a password — they have no other way
    in. Everyone else (own password, staff, OAuth…) gets the plain login page: a letter must
    not become a standing bearer credential for an account that has a real login.
    The fragment never reaches the server (no access logs, no Referer)."""
    from src.application.services.account_link import is_guest_account
    from src.web.routes.cabinet_auth import _auto_login_token

    if not cabinet:
        return ""
    if is_guest_account(user) and not user.role.is_staff:
        return f"{cabinet}#login={_auto_login_token(container, user.id)}"
    return cabinet


def render_html(text: str, *, brand: str, button: tuple[str, str] | None = None) -> str:
    """The HTML alternative of a plain-text letter: escaped, links clickable, one button."""

    def anchor(m: re.Match[str]) -> str:
        url = m.group(0)
        tail = ""
        while url and url[-1] in _TRAILING:
            tail = url[-1] + tail
            url = url[:-1]
        return f'<a href="{url}" style="color:#4c7dff;word-break:break-all">{url}</a>{tail}'

    body = _LINK.sub(anchor, html.escape(text, quote=True)).replace("\n", "<br>\n")
    cta = ""
    if button and button[1].startswith(("https://", "http://")):
        label, url = (html.escape(v, quote=True) for v in button)
        cta = (
            f'<p style="margin:24px 0 8px"><a href="{url}" style="display:inline-block;'
            "background:#4c7dff;color:#ffffff;text-decoration:none;font-weight:600;"
            f'padding:12px 22px;border-radius:10px">{label}</a></p>'
        )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:24px;background:#f3f4f7;'
        'font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1d2029">'
        '<div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:14px;'
        'padding:24px;font-size:15px;line-height:1.5">'
        f'<div style="font-weight:800;font-size:20px;margin-bottom:16px">'
        f"{html.escape(brand)}</div>"
        f"<div>{body}</div>{cta}</div></body></html>"
    )


async def send_template_email(
    container: AppContainer,
    to: str,
    event: str,
    *,
    button_label: str = "",
    button_url: str = "",
    **values: Any,
) -> bool:
    """Render the owner's letter ``event`` and send it (text + HTML). False when the owner
    disabled the letter, mail isn't configured or SMTP failed. Never raises."""
    from src.web.routes.admin.notifications import email_template

    try:
        async with container.uow() as uow:
            brand = await brand_title(uow)
            common = {
                "support": await support_contact(container, uow),
                "windows_app": await windows_app_text(container, uow, brand),
            }
            rendered = await email_template(uow, event, **{**common, **values})
        if rendered is None:
            return False  # the owner switched this letter off
        subject, text = rendered
        mailer = await container.build_mailer()
        button = (button_label, button_url) if button_label and button_url else None
        return await mailer.send(
            to, subject, text, html=render_html(text, brand=brand, button=button)
        )
    except Exception as exc:  # a letter is best-effort: never break the payment/reminder flow
        log.warning("template email failed", letter=event, error=str(exc))
        return False
