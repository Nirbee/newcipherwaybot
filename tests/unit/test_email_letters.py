"""Letters for people without Telegram: owner templates, HTML escaping, the payment letter
(which branch, which login link) and the expiry letters (same steps, no duplicates)."""

# ruff: noqa: RUF001  — RU letter texts are asserted verbatim.

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

from src.application.services.account_link import GUEST_FLAG
from src.application.services.bot_config import BotConfigService
from src.core.enums import (
    Currency,
    PurchaseType,
    Role,
    SubscriptionStatus,
    TransactionStatus,
    TransactionType,
    UserStatus,
)
from src.core.security import jwt_decode
from src.infrastructure.database.models.notification_template import NotificationTemplate
from src.infrastructure.database.models.reminder_step import ReminderStep
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.services.mailer import Mailer
from src.infrastructure.services.user_mail import cabinet_login_link, render_html
from src.infrastructure.taskiq import tasks
from src.web.routes.admin.notifications import EMAIL_EVENTS, email_template
from tests.factories import make_user

JWT_SECRET = "test-jwt-secret"
CABINET = "https://cabinet.example.ru/web/"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


class _FakeBot:
    sent: ClassVar[list[tuple[int, str]]] = []

    def __init__(self, token: str) -> None:
        self.session = SimpleNamespace(close=self._close)

    async def _close(self) -> None: ...

    async def send_message(self, chat_id: int, text: str, reply_markup: object = None) -> None:
        _FakeBot.sent.append((chat_id, text))


@pytest.fixture
def letters(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def fake_send(
        self: Mailer, to: str, subject: str, body: str, *, html: str | None = None
    ) -> bool:
        if not self.configured:
            return False
        sent.append({"to": to, "subject": subject, "body": body, "html": html or ""})
        return True

    monkeypatch.setattr(Mailer, "send", fake_send)
    return sent


@pytest.fixture
def env(uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    cfg = BotConfigService(None)

    async def build_mailer() -> Mailer:
        async with uow:
            return Mailer(
                host=str(await cfg.value(uow, "SMTP_HOST") or ""),
                port=int(await cfg.value(uow, "SMTP_PORT") or 587),
                user="",
                password="",
                from_addr=str(await cfg.value(uow, "SMTP_FROM") or ""),
            )

    dms: list[tuple[int, str]] = []

    async def notify_user(telegram_id: int, text: str) -> None:
        dms.append((telegram_id, text))

    container = SimpleNamespace(
        uow=lambda: uow,
        bot_config=cfg,
        build_mailer=build_mailer,
        redis=_FakeRedis(),
        notifier=SimpleNamespace(notify_user=notify_user),
        dms=dms,
        settings=SimpleNamespace(
            app=SimpleNamespace(jwt_secret=JWT_SECRET),
            bot=SimpleNamespace(token="42:TEST"),
            web=SimpleNamespace(public_url="https://cabinet.example.ru"),
        ),
    )
    monkeypatch.setattr(tasks, "get_container", lambda: cast("object", container))
    monkeypatch.setattr("aiogram.Bot", _FakeBot)
    _FakeBot.sent = []
    return container


async def _mail_on(uow: UnitOfWork, env: SimpleNamespace, **extra: Any) -> None:
    async with uow:
        await env.bot_config.set_values(
            uow, {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "noreply@example.com", **extra}
        )
        await uow.commit()


# --- templates + HTML ------------------------------------------------------------------


async def test_email_template_defaults_placeholders_and_off_switch(uow: UnitOfWork) -> None:
    async with uow:
        rendered = await email_template(
            uow, "renewal", plan="Стандарт", amount="300 ₽", expire="01.12.2026", support=""
        )
        assert rendered is not None
        subject, body = rendered
        assert subject == "Подписка продлена до 01.12.2026"
        assert "Тариф: Стандарт" in body and "Сумма: 300 ₽" in body
        assert "\n\n\n" not in body and not body.endswith("\n")  # empty {support} leaves no gap
        assert await email_template(uow, "no_such_letter") is None

        uow.session.add(NotificationTemplate(event="email:renewal", text="x", enabled=False))
        await uow.commit()
        assert await email_template(uow, "renewal", plan="Стандарт") is None  # owner switched off


async def test_email_subject_is_one_line(uow: UnitOfWork) -> None:
    async with uow:
        uow.session.add(
            NotificationTemplate(event="email:plan_changed:subject", text="Тариф {plan}")
        )
        await uow.commit()
        rendered = await email_template(uow, "plan_changed", plan="A\r\nBcc: evil@example.com")
    assert rendered is not None
    assert rendered[0] == "Тариф A Bcc: evil@example.com"  # no header injection


def test_email_events_are_unique_and_nonempty() -> None:
    events = [e for e, *_rest in EMAIL_EVENTS]
    assert len(events) == len(set(events))
    for _e, title, subject, body, _ph in EMAIL_EVENTS:
        assert title.strip() and subject.strip() and body.strip()


def test_render_html_escapes_everything_and_links_urls() -> None:
    text = 'Тариф: <script>alert(1)</script> & "x"\nСсылка: https://sub.example.ru/s/abc?x=1&y=2.'
    out = render_html(text, brand="<b>Brand</b>", button=("Войти <now>", "https://c.example.ru/"))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<b>Brand</b>" not in out and "&lt;b&gt;Brand&lt;/b&gt;" in out
    assert "&amp; &quot;x&quot;" in out
    assert '<a href="https://sub.example.ru/s/abc?x=1&amp;y=2"' in out  # trailing dot left out
    assert "Войти &lt;now&gt;" in out
    # a button may only point to http(s)
    assert "javascript" not in render_html("x", brand="B", button=("Go", "javascript:alert(1)"))


# --- login link in letters ---------------------------------------------------------------


def _user(**kw: Any) -> Any:
    return SimpleNamespace(id=7, role=Role.USER, notification_settings={}, **kw)


def test_login_link_is_one_click_only_for_guest_accounts() -> None:
    c = cast("Any", SimpleNamespace(settings=SimpleNamespace(app=SimpleNamespace(jwt_secret="s"))))
    guest = _user()
    guest.notification_settings = {GUEST_FLAG: True}
    link = cabinet_login_link(c, guest, CABINET)
    assert link.startswith(f"{CABINET}#login=")
    payload = jwt_decode(link.split("#login=", 1)[1], "s")
    assert payload is not None and payload["type"] == "auto_login" and payload["sub"] == 7

    assert cabinet_login_link(c, _user(), CABINET) == CABINET  # own password: login page
    staff = _user()
    staff.role, staff.notification_settings = Role.ADMIN, {GUEST_FLAG: True}
    assert cabinet_login_link(c, staff, CABINET) == CABINET  # staff: never a token
    assert cabinet_login_link(c, guest, "") == ""


# --- payment letter ----------------------------------------------------------------------


async def _paid(
    uow: UnitOfWork,
    *,
    telegram_id: int | None,
    purchase_type: PurchaseType = PurchaseType.NEW,
    guest: bool = True,
    plan: str = "Стандарт <b>",
) -> uuid.UUID:
    async with uow:
        user = await make_user(
            uow,
            telegram_id=telegram_id,
            email="buyer@example.com",
            email_verified=True,
            notification_settings={GUEST_FLAG: True} if guest else {},
        )
        await uow.session.flush()
        sub = Subscription(
            user_id=user.id,
            short_id=f"pay{uuid.uuid4().hex[:8]}",
            status=SubscriptionStatus.ACTIVE,
            expire_at=dt.datetime(2026, 12, 1, 12, tzinfo=dt.UTC),
            plan_snapshot={"name": plan},
            subscription_url="https://sub.example.ru/s/abc",
        )
        await uow.subscriptions.add(sub)
        await uow.session.flush()
        user.current_subscription_id = sub.id
        txn = Transaction(
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            status=TransactionStatus.COMPLETED,
            amount_minor=30000,
            currency=Currency.RUB,
            purchase_type=purchase_type,
        )
        uow.session.add(txn)
        await uow.commit()
        return txn.payment_id


async def test_paid_letter_for_guest_without_telegram(
    uow: UnitOfWork, env: SimpleNamespace, letters: list[dict[str, Any]]
) -> None:
    await _mail_on(uow, env, SUPPORT_REDIRECT_USERNAME="help_desk")
    pid = await _paid(uow, telegram_id=None)
    await tasks._notify_paid(env, pid)

    assert env.dms == []
    assert len(letters) == 1
    mail = letters[0]
    assert mail["to"] == "buyer@example.com"
    assert mail["subject"] == "Подписка оплачена — VPN готов к работе"
    body = mail["body"]
    assert "Тариф: Стандарт <b>" in body and "Сумма: 300 ₽" in body
    assert "Действует до: 01.12.2026" in body and "https://sub.example.ru/s/abc" in body
    assert "Happ, v2RayTun или Hiddify" in body and "Поддержка: https://t.me/help_desk" in body
    assert f"Войти в кабинет: {CABINET}#login=" in body  # guest: one-click login
    assert "Стандарт &lt;b&gt;" in mail["html"] and "Стандарт <b>" not in mail["html"]


async def test_paid_letter_registered_user_gets_login_page(
    uow: UnitOfWork, env: SimpleNamespace, letters: list[dict[str, Any]]
) -> None:
    await _mail_on(uow, env)
    pid = await _paid(uow, telegram_id=None, guest=False, purchase_type=PurchaseType.RENEW)
    await tasks._notify_paid(env, pid)
    assert len(letters) == 1
    assert letters[0]["subject"] == "Подписка продлена до 01.12.2026"
    assert letters[0]["body"].endswith(f"Войти в кабинет: {CABINET}")  # no support set
    assert "#login=" not in letters[0]["body"] + letters[0]["html"]


async def test_paid_telegram_user_gets_dm_not_letter(
    uow: UnitOfWork, env: SimpleNamespace, letters: list[dict[str, Any]]
) -> None:
    await _mail_on(uow, env)
    pid = await _paid(uow, telegram_id=5005)
    await tasks._notify_paid(env, pid)
    assert letters == [] and [tg for tg, _ in env.dms] == [5005]


async def test_paid_letter_switched_off_is_not_sent(
    uow: UnitOfWork, env: SimpleNamespace, letters: list[dict[str, Any]]
) -> None:
    await _mail_on(uow, env)
    async with uow:
        uow.session.add(NotificationTemplate(event="email:plan_changed", text="x", enabled=False))
        await uow.commit()
    pid = await _paid(uow, telegram_id=None, purchase_type=PurchaseType.CHANGE)
    await tasks._notify_paid(env, pid)
    assert letters == []


async def test_balance_topup_letter(
    uow: UnitOfWork, env: SimpleNamespace, letters: list[dict[str, Any]]
) -> None:
    await _mail_on(uow, env)
    async with uow:
        user = await make_user(uow, telegram_id=None, email="w@example.com", balance_minor=50000)
        await uow.session.flush()
        txn = Transaction(
            user_id=user.id,
            type=TransactionType.DEPOSIT,
            status=TransactionStatus.COMPLETED,
            amount_minor=20000,
            currency=Currency.RUB,
        )
        uow.session.add(txn)
        await uow.commit()
        pid = txn.payment_id
    await tasks._notify_paid(env, pid)
    assert [m["subject"] for m in letters] == ["Баланс пополнен на 200 ₽"]
    assert "Текущий баланс: 500 ₽." in letters[0]["body"]


# --- expiry letters ----------------------------------------------------------------------


async def _sub(
    uow: UnitOfWork,
    *,
    telegram_id: int | None,
    email: str | None,
    hours_left: float,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    user_status: UserStatus = UserStatus.ACTIVE,
) -> None:
    user = await make_user(uow, telegram_id=telegram_id, email=email, status=user_status)
    await uow.session.flush()
    sub = Subscription(
        user_id=user.id,
        short_id=f"exp{uuid.uuid4().hex[:8]}",
        status=status,
        expire_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours_left),
        plan_snapshot={"name": "Стандарт"},
    )
    await uow.subscriptions.add(sub)
    await uow.session.flush()
    user.current_subscription_id = sub.id


async def _ladder(uow: UnitOfWork) -> None:
    async with uow:
        uow.session.add(ReminderStep(hours_before=24, text="Истекает через {time}", enabled=True))
        uow.session.add(ReminderStep(hours_before=0, text="Подписка закончилась", enabled=True))
        await _sub(uow, telegram_id=None, email="soon@example.com", hours_left=23.95)
        await _sub(
            uow,
            telegram_id=None,
            email="gone@example.com",
            hours_left=-0.05,
            status=SubscriptionStatus.EXPIRED,
        )
        await _sub(uow, telegram_id=3003, email="tg@example.com", hours_left=23.95)
        await _sub(uow, telegram_id=None, email=None, hours_left=23.95)  # nowhere to write
        await _sub(
            uow,
            telegram_id=None,
            email="blocked@example.com",
            hours_left=23.95,
            user_status=UserStatus.BLOCKED,
        )
        await uow.commit()


async def test_expiry_letters_go_to_email_only_users_once(
    uow: UnitOfWork, env: SimpleNamespace, letters: list[dict[str, Any]]
) -> None:
    await _mail_on(uow, env)
    await _ladder(uow)
    assert await tasks.send_expiry_reminders() == 3  # two letters + one Telegram message

    by_to = {m["to"]: m for m in letters}
    assert set(by_to) == {"soon@example.com", "gone@example.com"}  # never the Telegram user
    assert by_to["soon@example.com"]["subject"] == "Подписка закончится через 24 часа"
    assert by_to["gone@example.com"]["subject"] == "Подписка закончилась"
    for mail in letters:
        assert CABINET in mail["body"] and f'href="{CABINET}"' in mail["html"]  # renew link
    assert [chat for chat, _ in _FakeBot.sent] == [3003]

    # the next 5-minute tick finds the same people — nobody gets a second letter
    letters.clear()
    _FakeBot.sent = []
    assert await tasks.send_expiry_reminders() == 0
    assert letters == [] and _FakeBot.sent == []


async def test_no_expiry_letters_without_mail_or_when_switched_off(
    uow: UnitOfWork, env: SimpleNamespace, letters: list[dict[str, Any]]
) -> None:
    await _ladder(uow)
    await tasks.send_expiry_reminders()  # SMTP not configured
    assert letters == []
    assert not any(k.startswith("exprem:mail:") for k in env.redis.store)  # nothing burned

    await _mail_on(uow, env)
    async with uow:
        uow.session.add(NotificationTemplate(event="email:expiring", text="x", enabled=False))
        await uow.commit()
    await tasks.send_expiry_reminders()
    assert [m["to"] for m in letters] == ["gone@example.com"]  # «закончилась» is still on


async def test_cabinet_url_for_letters(uow: UnitOfWork, env: SimpleNamespace) -> None:
    """A bare-domain CABINET_URL (older installs) still points letters at /web/, where the
    #reset / #login links are handled — not at the landing page."""
    from src.infrastructure.services.user_mail import cabinet_base_url

    c = cast("Any", env)
    async with uow:
        assert await cabinet_base_url(c, uow) == CABINET  # from the public URL
        for raw, expected in (
            ("https://vpn.example.ru", "https://vpn.example.ru/web/"),
            ("https://vpn.example.ru/", "https://vpn.example.ru/web/"),
            ("https://vpn.example.ru/cab", "https://vpn.example.ru/cab/"),
        ):
            await env.bot_config.set_values(uow, {"CABINET_URL": raw})
            assert await cabinet_base_url(c, uow) == expected


@pytest.mark.parametrize("port", [465, 587])
def test_mailer_envelope_is_exactly_the_recipient(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SMTP envelope goes to ``to`` only (SSL and STARTTLS alike) and the letter is
    multipart: plain text plus the HTML alternative."""
    from src.infrastructure.services import mailer as mailer_mod

    calls: list[tuple[Any, Any]] = []

    class FakeSMTP:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *_exc: object) -> None: ...

        def starttls(self) -> None: ...

        def login(self, *_a: Any) -> None: ...

        def send_message(self, msg: Any, to_addrs: Any = None) -> None:
            calls.append((msg, to_addrs))

    monkeypatch.setattr(mailer_mod, "_SMTP", FakeSMTP)
    monkeypatch.setattr(mailer_mod, "_SMTP_SSL", FakeSMTP)
    m = Mailer(
        host="smtp.example.com", port=port, user="u", password="p", from_addr="x@example.com"
    )
    m._send_sync("buyer@example.com", "Тема", "текст", "<p>текст</p>")
    msg, to_addrs = calls[0]
    assert to_addrs == ["buyer@example.com"]
    assert msg.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in msg.get_payload()] == ["text/plain", "text/html"]
