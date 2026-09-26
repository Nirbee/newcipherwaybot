"""Router customer onboarding: QR trial, claim by the scanning Telegram account, cash «paid on
site», @username lookup — all usable by a routers-only technician account.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import httpx

from src.application.dto.pricing import PurchaseRequest
from src.application.services import router_onboarding as onboarding
from src.core.enums import (
    Currency,
    PaymentGatewayType,
    PlanCategory,
    SubscriptionStatus,
    TransactionStatus,
)
from tests.factories import make_plan, make_user
from tests.integration.test_admin_api import ApiTestContainer, _login, client  # noqa: F401


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def notify_user(self, telegram_id: int, text: str) -> bool:
        self.sent.append((telegram_id, text))
        return True

    async def notify_admins(self, text: str, *, topic: str | None = None) -> None: ...

    async def notify_admins_document(self, document: object, *, caption: str | None = None) -> None:
        ...

    async def aclose(self) -> None: ...


async def _router_plans(container: ApiTestContainer) -> tuple[int, int]:
    """Base router plan (trial target) and a pricier family variant."""
    async with container.uow() as uow:
        base, _ = await make_plan(
            uow, code="router-base", name="Роутер", price_minor=50000, days=30,
            category=PlanCategory.ROUTER, device_limit=1, order_index=1,
        )
        family, _ = await make_plan(
            uow, code="router-plus2", name="Роутер + 2 чел.", price_minor=90000, days=30,
            category=PlanCategory.ROUTER, device_limit=3, order_index=2,
        )
        await container.bot_config.set_values(uow, {"BOT_USERNAME": "cipherway_bot"})
        await uow.commit()
        return base.id, family.id


async def _create_qr_router(http: httpx.AsyncClient, auth: dict[str, str]) -> dict:
    res = await http.post("/api/admin/routers", headers=auth, json={"label": "Ивановы"})
    assert res.status_code == 200, res.text
    return res.json()


async def test_qr_router_gets_trial_and_claim_link(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    base_id, _ = await _router_plans(container)
    body = await _create_qr_router(http, await _login(http))

    assert body["claim_url"].startswith("https://t.me/cipherway_bot?start=router_")
    assert body["subscription"]["is_trial"] is True
    async with container.uow() as uow:
        device = await uow.router_devices.get(body["id"])
        sub = await uow.subscriptions.get(device.subscription_id)
        owner = await uow.users.get(sub.user_id)
    assert device.claim_code is not None
    assert sub.plan_id == base_id and sub.status is SubscriptionStatus.TRIAL
    assert owner.telegram_id is None
    # The router trial is the technician's — it must not burn the person's own app trial.
    assert owner.is_trial_available is True


async def test_qr_router_without_router_plan_is_rejected(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, _ = client
    res = await http.post(
        "/api/admin/routers", headers=await _login(http), json={"label": "Без тарифа"}
    )
    assert res.status_code == 400


async def test_scan_binds_router_and_trial_to_the_telegram_account(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _router_plans(container)
    body = await _create_qr_router(http, await _login(http))
    code = body["claim_url"].rsplit("router_", 1)[1]

    async with container.uow() as uow:
        customer = await make_user(uow, telegram_id=5550001, username="ivanov")
        await uow.commit()
        device = await onboarding.claim(
            uow,
            subscriptions=container.subscriptions,
            remnawave=container.remnawave,
            user=customer,
            code=code,
        )
        await uow.commit()
        sub = await uow.subscriptions.get(device.subscription_id)
        customer = await uow.users.get(customer.id)
    assert device.claim_code is None
    assert sub.user_id == customer.id
    assert customer.current_subscription_id == sub.id

    async with container.uow() as uow:
        again = await uow.users.get(customer.id)
        try:
            await onboarding.claim(
                uow,
                subscriptions=container.subscriptions,
                remnawave=container.remnawave,
                user=again,
                code=code,
            )
            reused = True
        except onboarding.RouterOnboardingError:
            reused = False
    assert reused is False


async def test_scan_by_customer_with_own_subscription_joins_it(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _router_plans(container)
    body = await _create_qr_router(http, await _login(http))
    code = body["claim_url"].rsplit("router_", 1)[1]

    async with container.uow() as uow:
        customer = await make_user(uow, telegram_id=5550002)
        app_plan, _ = await make_plan(uow, code="app-plan")
        await uow.commit()
        req = PurchaseRequest(
            user_id=customer.id, plan_id=app_plan.id, duration_days=30, currency=Currency.RUB
        )
        own = await container.subscriptions.grant(uow, user=customer, plan=app_plan, req=req)
        await uow.commit()
        own_id = own.id
        device = await onboarding.claim(
            uow,
            subscriptions=container.subscriptions,
            remnawave=container.remnawave,
            user=customer,
            code=code,
        )
        await uow.commit()
        trial = await uow.subscriptions.get(body["subscription"]["id"])
    assert device.subscription_id == own_id
    assert trial.status is SubscriptionStatus.EXPIRED


async def test_paid_on_site_is_a_cash_sale_that_switches_to_the_family_plan(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    _, family_id = await _router_plans(container)
    auth = await _login(http)
    body = await _create_qr_router(http, auth)

    res = await http.post(
        f"/api/admin/routers/{body['id']}/paid",
        headers=auth,
        json={"plan_id": family_id, "days": 30},
    )
    assert res.status_code == 200, res.text
    assert res.json()["amount_minor"] == 90000
    summary = res.json()["subscription"]
    assert summary["is_trial"] is False and summary["status"] == "active"
    assert summary["device_limit"] == 3

    async with container.uow() as uow:
        device = await uow.router_devices.get(body["id"])
        sub = await uow.subscriptions.get(device.subscription_id)
        txns = await uow.transactions.list(user_id=sub.user_id)
    assert sub.plan_id == family_id
    paid = [t for t in txns if t.status is TransactionStatus.COMPLETED]
    assert len(paid) == 1
    assert paid[0].gateway_type is PaymentGatewayType.MANUAL
    assert paid[0].amount_minor == 90000


async def test_paid_on_site_notifies_a_claimed_customer(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    base_id, _ = await _router_plans(container)
    auth = await _login(http)
    body = await _create_qr_router(http, auth)
    code = body["claim_url"].rsplit("router_", 1)[1]
    async with container.uow() as uow:
        customer = await make_user(uow, telegram_id=5550003)
        await uow.commit()
        await onboarding.claim(
            uow,
            subscriptions=container.subscriptions,
            remnawave=container.remnawave,
            user=customer,
            code=code,
        )
        await uow.commit()

    notifier = _RecordingNotifier()
    container.notifier = notifier
    res = await http.post(
        f"/api/admin/routers/{body['id']}/paid",
        headers=auth,
        json={"plan_id": base_id, "days": 30},
    )
    assert res.status_code == 200, res.text
    assert notifier.sent and notifier.sent[0][0] == 5550003
    assert "Оплата получена" in notifier.sent[0][1]


async def test_existing_customer_by_username_gets_trial_and_a_message(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    await _router_plans(container)
    auth = await _login(http)
    async with container.uow() as uow:
        await make_user(uow, telegram_id=5550004, username="Alex_Berk")
        await uow.commit()

    found = await http.get("/api/admin/routers/customers?q=@alex_berk", headers=auth)
    items = found.json()["items"]
    assert [i["username"] for i in items] == ["Alex_Berk"]

    notifier = _RecordingNotifier()
    container.notifier = notifier
    res = await http.post(
        "/api/admin/routers",
        headers=auth,
        json={"label": "Берк", "user_id": items[0]["id"]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["claim_url"] is None
    assert res.json()["subscription"]["is_trial"] is True
    assert notifier.sent[0][0] == 5550004
    assert "start=routerplans" in notifier.sent[0][1]


async def test_routers_only_technician_can_onboard_end_to_end(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    base_id, _ = await _router_plans(container)
    owner = await _login(http)
    await http.post(
        "/api/admin/admins",
        headers=owner,
        json={"username": "tech", "password": "TechPass123!", "allowed_screens": ["routers"]},
    )
    login = await http.post(
        "/api/admin/auth/login", json={"username": "tech", "password": "TechPass123!"}
    )
    tech = {"Authorization": f"Bearer {login.json()['token']}"}

    assert (await http.get("/api/admin/routers/plans", headers=tech)).status_code == 200
    assert (await http.get("/api/admin/routers/customers?q=abc", headers=tech)).status_code == 200
    created = await http.post("/api/admin/routers", headers=tech, json={"label": "Техник"})
    assert created.status_code == 200, created.text
    paid = await http.post(
        f"/api/admin/routers/{created.json()['id']}/paid",
        headers=tech,
        json={"plan_id": base_id, "days": 30},
    )
    assert paid.status_code == 200, paid.text
    # …while the wider customer base stays out of reach.
    assert (await http.get("/api/admin/users", headers=tech)).status_code == 403


class _FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, kwargs.get("reply_markup")))


async def test_bot_start_with_router_code_claims_and_offers_router_plans(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    from src.bot.handlers import start

    http, container = client
    base_id, family_id = await _router_plans(container)
    body = await _create_qr_router(http, await _login(http))
    code = body["claim_url"].rsplit("router_", 1)[1]
    async with container.uow() as uow:
        customer = await make_user(uow, telegram_id=5550010, username="petrov")
        await uow.commit()

    message = _FakeMessage()
    await start._router_claim(message, container, customer, code)  # type: ignore[arg-type]

    text, markup = message.answers[-1]
    assert "Ивановы" in text and "подключён" in text
    assert "Пробный период до" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]  # type: ignore[attr-defined]
    assert callbacks == [f"plan:{base_id}", f"plan:{family_id}", "nav:root"]
    labels = [b.text for row in markup.inline_keyboard for b in row]  # type: ignore[attr-defined]
    assert labels[1].startswith("Роутер + 2 чел. · 3 устр.")

    async with container.uow() as uow:
        device = await uow.router_devices.get(body["id"])
        sub = await uow.subscriptions.get(device.subscription_id)
    assert sub.user_id == customer.id and device.claim_code is None


async def test_plans_are_appended_in_order_and_can_be_reordered(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    auth = await _login(http)
    ids = []
    for name in ("Router +2", "Router +10", "Router +5"):
        res = await http.post(
            "/api/admin/plans",
            headers=auth,
            json={
                "name": name,
                "category": "router",
                "durations": [{"days": 30, "price_minor": 49000}],
            },
        )
        assert res.status_code == 200, res.text
        ids.append(res.json()["id"])

    listed = [p["id"] for p in (await http.get("/api/admin/plans", headers=auth)).json()["items"]]
    assert listed == ids  # new plans land at the end, in creation order

    wanted = [ids[0], ids[2], ids[1]]
    res = await http.put("/api/admin/plans/order", headers=auth, json={"ids": wanted})
    assert res.status_code == 200
    listed = [p["id"] for p in (await http.get("/api/admin/plans", headers=auth)).json()["items"]]
    assert listed == wanted
    async with container.uow() as uow:
        assert [p.id for p in await onboarding.router_plans(uow)] == wanted


async def test_dashboard_period_revenue_split_and_delta(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    import datetime as dt

    from src.core.enums import PurchaseType, TransactionType
    from src.infrastructure.database.models.transaction import Transaction

    http, container = client
    base_id, _ = await _router_plans(container)
    now = dt.datetime.now(dt.UTC)
    async with container.uow() as uow:
        buyer = await make_user(uow, telegram_id=5559001)
        rows = [
            # current period: a router sale by card + a balance top-up
            (TransactionType.SUBSCRIPTION_PAYMENT, 50000, now, {"plan_id": base_id}),
            (TransactionType.DEPOSIT, 20000, now - dt.timedelta(days=1), None),
            # previous period (8-13 days ago for days=7)
            (TransactionType.DEPOSIT, 10000, now - dt.timedelta(days=10), None),
        ]
        for ttype, amount, at, snapshot in rows:
            await uow.transactions.add(
                Transaction(
                    user_id=buyer.id,
                    type=ttype,
                    status=TransactionStatus.COMPLETED,
                    amount_minor=amount,
                    currency=Currency.RUB,
                    gateway_type=PaymentGatewayType.MANUAL,
                    purchase_type=PurchaseType.NEW if snapshot else None,
                    plan_snapshot=snapshot,
                    completed_at=at,
                )
            )
        await uow.commit()

    body = (await http.get("/api/admin/dashboard?days=7", headers=await _login(http))).json()
    rev = body["revenue"]
    assert body["days"] == 7 and len(rev["series"]) == 7
    assert rev["current_minor"] == 70000 and rev["previous_minor"] == 10000
    assert rev["by_product"]["router"] == 50000 and rev["by_product"]["topup"] == 20000
    assert rev["orders"] == 2 and rev["payers"] == 1 and rev["avg_check_minor"] == 35000
    assert rev["series"][-1]["amount_minor"] == 50000
    assert body["routers"] == {"total": 0, "online": 0, "offline": 0, "pending": 0}
