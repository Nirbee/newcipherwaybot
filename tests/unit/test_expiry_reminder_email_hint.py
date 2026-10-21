"""Expiry reminder: «Привяжите почту» — once per expiry, only to people without an e-mail,
and only when the site and the mailer can actually serve it."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

from src.application.services.bot_config import BotConfigService
from src.core.enums import SubscriptionStatus
from src.infrastructure.database.models.reminder_step import ReminderStep
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.taskiq import tasks
from tests.factories import make_user


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True


class _FakeBot:
    sent: ClassVar[list[tuple[int, str, object]]] = []

    def __init__(self, token: str) -> None:
        self.session = SimpleNamespace(close=self._close)

    async def _close(self) -> None: ...

    async def send_message(self, chat_id: int, text: str, reply_markup: object = None) -> None:
        _FakeBot.sent.append((chat_id, text, reply_markup))


async def _sub(uow: UnitOfWork, *, telegram_id: int, hours_left: float, email: bool) -> None:
    user = await make_user(uow, telegram_id=telegram_id)
    if email:
        user.email = f"u{telegram_id}@example.com"
        user.email_verified = True
    sub = Subscription(
        user_id=user.id,
        short_id=f"rem{telegram_id}"[:16],
        status=SubscriptionStatus.ACTIVE,
        expire_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours_left),
        plan_snapshot={"name": "Стандартный"},
    )
    await uow.subscriptions.add(sub)
    await uow.session.flush()
    user.current_subscription_id = sub.id


@pytest.fixture
def env(uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch) -> BotConfigService:
    cfg = BotConfigService(None)
    container = SimpleNamespace(
        uow=lambda: uow,
        bot_config=cfg,
        redis=_FakeRedis(),
        settings=SimpleNamespace(
            bot=SimpleNamespace(token="42:TEST"),
            web=SimpleNamespace(public_url="https://cabinet.example.ru"),
        ),
    )
    monkeypatch.setattr(tasks, "get_container", lambda: cast("object", container))
    monkeypatch.setattr("aiogram.Bot", _FakeBot)
    _FakeBot.sent = []
    return cfg


async def _setup(uow: UnitOfWork, cfg: BotConfigService, *, mail: bool) -> None:
    async with uow:
        uow.session.add(ReminderStep(hours_before=24, text="Истекает через {time}", enabled=True))
        uow.session.add(ReminderStep(hours_before=1, text="Остался час", enabled=True))
        await cfg.set_values(
            uow,
            {
                "WEB_CABINET_ENABLED": True,
                "SUBSCRIPTION_MINI_APP_URL": "https://cabinet.example.ru/app/",
                **({"SMTP_HOST": "smtp.yandex.ru", "SMTP_FROM": "x@yandex.ru"} if mail else {}),
            },
        )
        await _sub(uow, telegram_id=1001, hours_left=23.95, email=False)  # 24h step, no e-mail
        await _sub(uow, telegram_id=1002, hours_left=23.95, email=True)  # 24h step, has e-mail
        await _sub(uow, telegram_id=1003, hours_left=0.95, email=False)  # 1h step: no repeat
        await uow.commit()


async def test_hint_goes_once_to_people_without_email(
    uow: UnitOfWork, env: BotConfigService
) -> None:
    await _setup(uow, env, mail=True)
    assert await tasks.send_expiry_reminders() == 3
    by_id = {chat: (text, markup) for chat, text, markup in _FakeBot.sent}

    text, markup = by_id[1001]
    assert "Привяжите почту" in text and "/web/?renew=1" in text
    buttons = [b.text for row in markup.inline_keyboard for b in row]
    assert buttons == ["Продлить", "✉️ Привязать почту"]
    assert markup.inline_keyboard[1][0].web_app.url.endswith("/app/?tab=account")

    assert "Привяжите почту" not in by_id[1002][0]  # already linked
    assert "Привяжите почту" not in by_id[1003][0]  # only the earliest step carries it


async def test_no_hint_while_mail_is_not_configured(uow: UnitOfWork, env: BotConfigService) -> None:
    await _setup(uow, env, mail=False)
    await tasks.send_expiry_reminders()
    assert all("Привяжите почту" not in text for _c, text, _m in _FakeBot.sent)
