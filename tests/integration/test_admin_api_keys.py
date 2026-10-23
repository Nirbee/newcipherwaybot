# ruff: noqa: F811 — the imported `client` fixture is (by design) a test parameter below.
"""Read-only admin API keys (cwk_...): issue/revoke, GET-only, closed screens, rate limit,
masking of e-mails/phones, per-key journal.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import datetime as dt
import gzip
from typing import Any

import httpx
import pytest

from src.application.services.ids import generate_referral_code
from src.core.enums import AuthType, Role
from src.core.security import hash_password
from src.infrastructure.database.models.user import User
from src.infrastructure.database.models.withdrawal import WithdrawalRequest
from src.web.routes.admin.api_key_auth import (
    KEY_BLOCKED_SCREENS,
    KEY_RATE_LIMIT,
    KEY_STATE_ATTR,
    AdminKeyResponseFilter,
)
from src.web.routes.admin.api_keys import KEY_SCREENS
from tests.integration.test_admin_api import (  # noqa: F401
    ApiTestContainer,
    _FakeRedis,
    _login,
    client,
)


class _ListRedis(_FakeRedis):
    """The shared fake plus the list commands the key journal uses."""

    def __init__(self) -> None:
        super().__init__()
        self.lists: dict[str, list[str]] = {}
        self.ttls: dict[str, int] = {}

    async def lpush(self, key: str, *values: str) -> int:
        lst = self.lists.setdefault(key, [])
        for v in values:
            lst.insert(0, v)
        return len(lst)

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        self.lists[key] = self.lists.get(key, [])[start : end + 1]
        return True

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.lists.get(key, [])[start : end + 1]

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        return _Pipeline(self)


class _Pipeline:
    """MULTI for the rate limiter: INCR + EXPIRE NX, executed together (TTLs recorded)."""

    def __init__(self, redis: _ListRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, str, int]] = []

    def incr(self, key: str) -> None:
        self.ops.append(("incr", key, 0))

    def expire(self, key: str, seconds: int, nx: bool = False) -> None:
        assert nx, "the limiter must only set a TTL on a counter that has none"
        self.ops.append(("expire", key, seconds))

    async def execute(self) -> list[Any]:
        out: list[Any] = []
        for op, key, seconds in self.ops:
            if op == "incr":
                out.append(await self.redis.incr(key))
            else:
                self.redis.ttls.setdefault(key, seconds)
                out.append(True)
        return out


async def _issue(
    http: httpx.AsyncClient, owner: dict[str, str], **body: Any
) -> tuple[str, dict[str, Any]]:
    res = await http.post("/api/admin/admin-keys", headers=owner, json={"name": "Claude", **body})
    assert res.status_code == 200, res.text
    return res.json()["key"], res.json()["item"]


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_key_issued_once_and_reads_only(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.redis = _ListRedis()
    owner = await _login(http)
    key, item = await _issue(http, owner)

    assert key.startswith("cwk_") and len(key) == 44
    assert item["prefix"] == key[:8] and item["status"] == "active"
    expires = dt.datetime.fromisoformat(item["expires_at"])
    assert dt.timedelta(days=89) < expires - dt.datetime.now(dt.UTC) <= dt.timedelta(days=90)
    # the list never shows the key again
    listing = (await http.get("/api/admin/admin-keys", headers=owner)).json()
    assert key not in str(listing)
    assert listing["items"][0]["name"] == "Claude"

    auth = _bearer(key)
    assert (await http.get("/api/admin/dashboard", headers=auth)).status_code == 200
    me = (await http.get("/api/admin/auth/me", headers=auth)).json()
    assert me["username"] == "ключ: Claude"
    assert me["role"] == "PREVIEW"

    res = await http.post("/api/admin/servers/sync", headers=auth)
    assert res.status_code == 403
    assert res.json()["detail"] == "ключ только для чтения"
    res = await http.patch(
        "/api/admin/settings", headers=auth, json={"changes": {"TRIAL_ENABLED": False}}
    )
    assert res.status_code == 403
    assert (await http.delete("/api/admin/blacklist/1", headers=auth)).status_code == 403
    # a key can't issue keys (or touch any owner-only screen)
    res = await http.post("/api/admin/admin-keys", headers=auth, json={"name": "x"})
    assert res.status_code == 403

    # last_used_at is recorded
    listing = (await http.get("/api/admin/admin-keys", headers=owner)).json()
    assert listing["items"][0]["last_used_at"] is not None


async def test_closed_screens_and_allowed_screens(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.redis = _ListRedis()
    owner = await _login(http)
    key, _ = await _issue(http, owner)
    auth = _bearer(key)

    for path in (
        "/api/admin/admins",
        "/api/admin/admin-keys",
        "/api/admin/site",
        "/api/admin/providers",
        "/api/admin/bot-menu/export",
    ):
        res = await http.get(path, headers=auth)
        assert res.status_code == 403, path
    # the rest of a screen with one closed endpoint stays readable
    assert (await http.get("/api/admin/bot-menu", headers=auth)).status_code == 200

    scoped, item = await _issue(http, owner, allowed_screens=["routers", "bogus", "admins"])
    assert item["allowed_screens"] == ["routers"]  # unknown/closed screens are dropped
    sauth = _bearer(scoped)
    assert (await http.get("/api/admin/routers", headers=sauth)).status_code == 200
    assert (await http.get("/api/admin/auth/me", headers=sauth)).status_code == 200
    assert (await http.get("/api/admin/users", headers=sauth)).status_code == 403


async def test_revoked_expired_wrong_and_demoted_creator_401(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.redis = _ListRedis()
    owner = await _login(http)

    wrong = "cwk_" + "A" * 40
    assert (await http.get("/api/admin/dashboard", headers=_bearer(wrong))).status_code == 401

    key, item = await _issue(http, owner)
    res = await http.post(f"/api/admin/admin-keys/{item['id']}/revoke", headers=owner)
    assert res.status_code == 200
    assert (await http.get("/api/admin/dashboard", headers=_bearer(key))).status_code == 401
    listing = (await http.get("/api/admin/admin-keys", headers=owner)).json()
    assert listing["items"][0]["status"] == "revoked"

    key2, item2 = await _issue(http, owner, days=30)
    async with container.uow() as uow:
        row = await uow.admin_api_keys.get(item2["id"])
        assert row is not None
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await uow.commit()
    assert (await http.get("/api/admin/dashboard", headers=_bearer(key2))).status_code == 401

    # the key dies with its creator's owner rights
    key3, _ = await _issue(http, owner, days=365)
    assert (await http.get("/api/admin/dashboard", headers=_bearer(key3))).status_code == 200
    async with container.uow() as uow:
        creator = await uow.users.find_one(username="root_admin")
        assert creator is not None
        creator.role = Role.ADMIN
        await uow.commit()
    assert (await http.get("/api/admin/dashboard", headers=_bearer(key3))).status_code == 401


async def test_rate_limit_429(client: tuple[httpx.AsyncClient, ApiTestContainer]) -> None:
    http, container = client
    container.redis = _ListRedis()
    owner = await _login(http)
    key, item = await _issue(http, owner)
    # one short of the limit: the next request is the last one allowed (counted once)
    container.redis.store[f"rl:admin_key:{item['id']}"] = str(KEY_RATE_LIMIT - 1)
    assert (await http.get("/api/admin/dashboard", headers=_bearer(key))).status_code == 200
    assert (await http.get("/api/admin/dashboard", headers=_bearer(key))).status_code == 429
    # the window always has a TTL (set in the same MULTI as the INCR)
    assert container.redis.ttls[f"rl:admin_key:{item['id']}"] == 60


async def test_key_rejected_outside_admin_api(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.redis = _ListRedis()
    owner = await _login(http)
    key, _ = await _issue(http, owner)
    auth = _bearer(key)
    assert (await http.get("/api/cabinet/me", headers=auth)).status_code == 401
    assert (await http.get("/api/agent/config", headers=auth)).status_code == 401


async def test_key_responses_mask_email_and_phone_staff_unchanged(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.redis = _ListRedis()
    owner = await _login(http)
    res = await http.patch(
        "/api/admin/settings",
        headers=owner,
        json={"changes": {"SMTP_USER": "alice@ya.ru", "SMTP_FROM": "Поддержка +7 999 123-45-12"}},
    )
    assert res.status_code == 200, res.text
    key, _ = await _issue(http, owner)

    def values(body: dict[str, Any]) -> dict[str, Any]:
        return {p["key"]: p["value"] for p in body["params"]}

    staff = values((await http.get("/api/admin/settings?q=SMTP", headers=owner)).json())
    assert staff["SMTP_USER"] == "alice@ya.ru"
    assert staff["SMTP_FROM"] == "Поддержка +7 999 123-45-12"

    res = await http.get("/api/admin/settings?q=SMTP", headers=_bearer(key))
    assert res.status_code == 200
    assert int(res.headers["content-length"]) == len(res.content)
    masked = values(res.json())
    assert masked["SMTP_USER"] == "a***@ya.ru"
    assert masked["SMTP_FROM"] == "Поддержка +7***12"
    assert "alice" not in res.text


async def test_key_journal_and_owner_only_screen(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.redis = _ListRedis()
    owner = await _login(http)
    key, item = await _issue(http, owner)
    auth = _bearer(key)
    await http.get("/api/admin/dashboard?days=7", headers=auth)
    await http.post("/api/admin/servers/sync", headers=auth)

    res = await http.get(f"/api/admin/admin-keys/{item['id']}/log", headers=owner)
    assert res.status_code == 200
    log = res.json()["items"]
    assert [(e["method"], e["path"], e["status"]) for e in log] == [
        ("POST", "/api/admin/servers/sync", 403),
        ("GET", "/api/admin/dashboard?days=7", 200),
    ]
    assert log[0]["at"]

    # a plain admin can't see or manage keys
    async with container.uow() as uow:
        await uow.users.add(
            User(
                username="plain_admin",
                auth_type=AuthType.EMAIL,
                role=Role.ADMIN,
                referral_code=generate_referral_code(),
                password_hash=hash_password("PlainAdmin123!"),
            )
        )
        await uow.commit()
    login = await http.post(
        "/api/admin/auth/login", json={"username": "plain_admin", "password": "PlainAdmin123!"}
    )
    admin = {"Authorization": f"Bearer {login.json()['token']}"}
    assert (await http.get("/api/admin/admin-keys", headers=admin)).status_code == 403
    res = await http.post("/api/admin/admin-keys", headers=admin, json={"name": "x"})
    assert res.status_code == 403


def test_selectable_screens_never_include_closed_ones() -> None:
    assert not set(KEY_SCREENS) & KEY_BLOCKED_SCREENS


async def test_key_masks_promocodes_payout_details_and_panel_url(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    container.redis = _ListRedis()
    monkeypatch.setattr(container.settings.remnawave, "base_url", "https://panel.example.com")
    owner = await _login(http)
    res = await http.post(
        "/api/admin/promocodes",
        headers=owner,
        json={"code": "SUMMER2026", "reward_type": "balance", "reward_value": 100},
    )
    assert res.status_code == 200, res.text
    async with container.uow() as uow:
        payee = User(
            username="payee",
            auth_type=AuthType.EMAIL,
            role=Role.USER,
            referral_code=generate_referral_code(),
        )
        await uow.users.add(payee)
        await uow.withdrawals.add(
            WithdrawalRequest(
                user_id=payee.id, amount_minor=50000, method="card", details="2200 1234 5678 9012"
            )
        )
        await uow.commit()
    key, _ = await _issue(http, owner)
    auth = _bearer(key)

    # staff see everything as before
    staff_promos = (await http.get("/api/admin/promocodes", headers=owner)).json()
    assert staff_promos["items"][0]["code"] == "SUMMER2026"
    staff_wd = (await http.get("/api/admin/withdrawals", headers=owner)).json()
    assert staff_wd["items"][0]["details"] == "2200 1234 5678 9012"

    promos = (await http.get("/api/admin/promocodes", headers=auth)).json()
    assert promos["items"][0]["code"] == "SU****"
    assert promos["items"][0]["used"] == 0  # activation stats stay visible
    assert "total_activations" in promos
    wd = (await http.get("/api/admin/withdrawals", headers=auth)).json()
    assert wd["items"][0]["details"] == "****9012"
    assert wd["items"][0]["username"] == "payee"
    assert (await http.get("/api/admin/servers", headers=owner)).json()["panel_url"] == (
        "https://panel.example.com"
    )
    assert (await http.get("/api/admin/servers", headers=auth)).json()["panel_url"] == "***"
    # report-topics' own «code» field is not a promo code — left alone there
    topics = (await http.get("/api/admin/report-topics", headers=auth)).json()
    assert any(t["code"] == "daily_report" for t in topics["items"])

    # the heavy whole-panel pull is closed to keys
    assert (await http.get("/api/admin/users/panel-sync", headers=auth)).status_code == 403


async def test_key_response_filter_fails_closed() -> None:
    """A key never gets a body the filter couldn't check: non-JSON, compressed or broken JSON
    becomes a 502 with a JSON detail. Staff answers through the same filter are untouched."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route

    def as_key(request: Request) -> None:
        if request.headers.get("x-key"):
            setattr(request.state, KEY_STATE_ATTR, 1)

    async def text(request: Request) -> Response:
        as_key(request)
        return PlainTextResponse("alice@ya.ru")

    async def gzipped(request: Request) -> Response:
        as_key(request)
        return Response(
            gzip.compress(b'{"email": "alice@ya.ru"}'),
            media_type="application/json",
            headers={"content-encoding": "gzip"},
        )

    async def broken(request: Request) -> Response:
        as_key(request)
        return Response(b'{"email": "alice@ya.ru"', media_type="application/json")

    async def good(request: Request) -> Response:
        as_key(request)
        return Response(b'{"email": "alice@ya.ru"}', media_type="application/json")

    app = Starlette(
        routes=[
            Route("/api/admin/text", text),
            Route("/api/admin/gz", gzipped),
            Route("/api/admin/broken", broken),
            Route("/api/admin/good", good),
        ]
    )
    app.add_middleware(AdminKeyResponseFilter)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        for path in ("/api/admin/text", "/api/admin/gz", "/api/admin/broken"):
            res = await http.get(path, headers={"x-key": "1"})
            assert res.status_code == 502, path
            assert res.json() == {"detail": "ответ нельзя безопасно отдать по ключу"}
            assert "alice" not in res.text
            # without a key the very same answer passes as is
            assert (await http.get(path)).status_code == 200
        res = await http.get("/api/admin/good", headers={"x-key": "1"})
        assert res.status_code == 200
        assert res.json() == {"email": "a***@ya.ru"}
