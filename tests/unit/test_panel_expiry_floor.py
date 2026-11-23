"""The panel's expiry is never moved back by the bot.

Field case (Groketi, 24.11): the owner extended a customer in the panel directly by a year; the
bot still had the old, expired date. A 30-day payment in the bot counted from now and would
have pushed an expiry ~10 months SHORTER than the panel's. Renewals count from the latest of
now / the bot's expiry / the panel's expiry, and no write (except an admin's explicit «до
даты» or a grace window) sends an earlier expiry than the panel has.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from src.application.dto.pricing import PurchaseRequest
from src.application.services.remnawave import RemnawaveService
from src.application.services.subscription import SubscriptionService
from src.core.enums import Currency, SubscriptionStatus
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.uow import UnitOfWork
from tests.factories import make_plan, make_user
from tests.fakes import FakeRemnawaveClient


async def _sub_with_panel_year_ahead(
    uow: UnitOfWork,
) -> tuple[FakeRemnawaveClient, SubscriptionService, Subscription, dt.datetime]:
    fake = FakeRemnawaveClient()
    subs = SubscriptionService(RemnawaveService(fake))
    user = await make_user(uow, telegram_id=7007)
    plan, _ = await make_plan(uow)
    await uow.commit()
    req = PurchaseRequest(user_id=user.id, plan_id=plan.id, duration_days=30, currency=Currency.RUB)
    sub = await subs.grant(uow, user=user, plan=plan, req=req)
    assert sub.remnawave_uuid is not None
    # the bot thinks it ran out 45 days ago; the owner extended it in the panel by a year
    now = dt.datetime.now(dt.UTC)
    sub.expire_at = now - dt.timedelta(days=45)
    sub.status = SubscriptionStatus.EXPIRED
    panel_end = now + dt.timedelta(days=365)
    fake.users[sub.remnawave_uuid] = dataclasses.replace(
        fake.users[sub.remnawave_uuid], expire_at=panel_end
    )
    await uow.commit()
    return fake, subs, sub, panel_end


async def test_paying_30_days_adds_to_the_panels_later_expiry(uow: UnitOfWork) -> None:
    async with uow:
        fake, subs, sub, panel_end = await _sub_with_panel_year_ahead(uow)
        await subs.renew(uow, sub, days=30)
        expected = panel_end + dt.timedelta(days=30)
        assert sub.expire_at == expected  # panel's + 30, not now + 30
        assert sub.status is SubscriptionStatus.ACTIVE
        assert fake.users[sub.remnawave_uuid].expire_at == expected


async def test_other_writes_never_move_the_panel_expiry_back(uow: UnitOfWork) -> None:
    async with uow:
        fake, subs, sub, panel_end = await _sub_with_panel_year_ahead(uow)
        await subs.push_limits(uow, sub)  # «Привести панель к боту», the nightly heal
        assert fake.users[sub.remnawave_uuid].expire_at == panel_end
        assert sub.expire_at == panel_end  # the bot takes the panel's date over


async def test_admin_until_date_may_still_shorten(uow: UnitOfWork) -> None:
    async with uow:
        fake, subs, sub, _ = await _sub_with_panel_year_ahead(uow)
        target = dt.datetime.now(dt.UTC) + dt.timedelta(days=10)
        await subs.set_expiry(uow, sub, expire_at=target)  # the admin decides
        assert fake.users[sub.remnawave_uuid].expire_at == target


async def test_a_grace_window_does_not_count_as_the_base(uow: UnitOfWork) -> None:
    async with uow:
        fake, subs, sub, _ = await _sub_with_panel_year_ahead(uow)
        now = dt.datetime.now(dt.UTC)
        grace_end = now + dt.timedelta(days=2)
        sub.grace_until = grace_end
        fake.users[sub.remnawave_uuid] = dataclasses.replace(
            fake.users[sub.remnawave_uuid], expire_at=grace_end
        )
        await subs.renew(uow, sub, days=30)
        assert sub.expire_at is not None
        assert abs((sub.expire_at - (now + dt.timedelta(days=30))).total_seconds()) < 60


async def test_a_silent_panel_falls_back_to_the_bots_own_base(uow: UnitOfWork) -> None:
    async with uow:
        fake, subs, sub, _ = await _sub_with_panel_year_ahead(uow)

        async def down(ref: object) -> None:
            raise RuntimeError("panel down")

        fake.get_user = down  # type: ignore[method-assign,assignment]
        now = dt.datetime.now(dt.UTC)
        await subs.renew(uow, sub, days=30)
        assert sub.expire_at is not None
        assert abs((sub.expire_at - (now + dt.timedelta(days=30))).total_seconds()) < 60
