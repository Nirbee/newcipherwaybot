"""DevVersion API (/api/app/v1/telemetry) and its admin section (/api/admin/devversion): token
and live paid subscription, the allow-list, kill switch, size caps and the gzip bomb, the disk
guards (counts, bytes per account / for everyone, per IP, 500 rows per subscription, fail-closed
without Redis), checks, DELETE of one's own reports, the IP never stored, the retention and the
size alert, who sees raw payloads, and a read-only AI key that reads reports but can't export.

Reuses the ASGI-client fixture (fakes + in-memory sqlite) and the subscription helper from
test_app_api.py; the provider lookup is stubbed (no .mmdb in tests).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from src.application.services.ids import generate_referral_code
from src.core.enums import AuthType, Role, SubscriptionStatus
from src.core.security import hash_password
from src.infrastructure.database.dao.devversion import DevversionReportDAO
from src.infrastructure.database.models.devversion import DevversionReport
from src.infrastructure.database.models.user import User
from src.infrastructure.services import geoip
from src.infrastructure.services.devversion import STORAGE_FULL_KEY
from src.infrastructure.taskiq import tasks
from src.web.routes import app_telemetry
from src.web.routes.admin import devversion as admin_devversion
from src.web.routes.app_api import ip_key
from tests.integration.test_admin_api import ApiTestContainer, _login, client
from tests.integration.test_admin_api_keys import _bearer, _issue, _ListRedis
from tests.integration.test_app_api import _h, _sub

_FIXTURES = (client,)  # the shared ASGI-client fixture, re-exported (keeps ruff F401/F811 quiet)

Client = tuple[httpx.AsyncClient, ApiTestContainer]
BASE = "/api/app/v1/telemetry"
ADMIN = "/api/admin/devversion"
CLIENT_IP = "95.24.17.203"
FWD = {"X-Forwarded-For": CLIENT_IP}  # the ASGI test peer is 127.0.0.1 — a trusted proxy


class _DvPipe:
    """MULTI of the commands the limiters queue (INCR / INCRBY / EXPIRE NX), TTLs recorded."""

    def __init__(self, redis: _DvRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, str, int, bool]] = []

    def incr(self, key: str) -> None:
        self.ops.append(("incrby", key, 1, False))

    def incrby(self, key: str, amount: int) -> None:
        self.ops.append(("incrby", key, amount, False))

    def expire(self, key: str, seconds: int, nx: bool = False) -> None:
        self.ops.append(("expire", key, seconds, nx))

    async def execute(self) -> list[Any]:
        out: list[Any] = []
        for op, key, n, nx in self.ops:
            if op == "incrby":
                self.redis.store[key] = str(int(self.redis.store.get(key, "0")) + n)
                out.append(int(self.redis.store[key]))
            elif nx and key in self.redis.ttls:
                out.append(False)
            else:
                self.redis.ttls[key] = n
                out.append(True)
        return out


class _DvRedis(_ListRedis):
    """The shared fake plus MULTI with EXPIRE NX and the give-back Lua script."""

    def pipeline(self, transaction: bool = True) -> _DvPipe:  # type: ignore[override]
        return _DvPipe(self)

    async def eval(self, script: str, numkeys: int, *args: str) -> int:
        if "decrby" not in script:
            return await super().eval(script, numkeys, *args)
        key, amount = args[0], int(args[1])
        if key not in self.store:
            return 0
        n = max(0, int(self.store[key]) - amount)
        self.store[key] = str(n)
        return n


@pytest.fixture(autouse=True)
def _redis(client: Client) -> _DvRedis:
    redis = _DvRedis()
    client[1].redis = redis  # type: ignore[assignment]
    return redis


@pytest.fixture(autouse=True)
def _geo(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def fake(directory: str, ip: str) -> geoip.GeoInfo:
        asked.append(ip)
        return geoip.GeoInfo(
            asn=8402, as_org="PJSC Vimpelcom", country="RU", region="Москва", city="Москва"
        )

    monkeypatch.setattr(app_telemetry.geoip, "lookup", fake)
    return asked


def _report(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": 1,
        "consent_version": "1",
        "install_id": str(uuid.uuid4()),
        "app_version": "0.5.0",
        "os": {"build": "19045", "edition": "Pro"},
        "mode": "tun",
        "period": {"from": "2026-11-23T10:00:00Z", "to": "2026-11-23T11:00:00Z"},
        "system": {"dns": ["1.1.1.1"]},
        "apps": [{"name": "Discord", "direct_failures": 2}],
        "probes": [{"target": "youtube.com", "route": "direct", "ok": False}],
        "nodes": [{"node": "Германия", "protocol": "vless", "latency_ms": 42, "loss_pct": 0}],
        "events": [{"level": "info", "msg": "connected"}],
        "game_tests": [{"game": "PUBG", "route": "vpn", "latency_ms": 70}],
    }
    body.update(overrides)
    return body


async def _player(
    container: ApiTestContainer, n: int, **kw: Any
) -> tuple[dict[str, str], int, int]:
    """(headers, subscription id, user id) of a fresh customer with a live paid subscription."""
    token = f"DevTok{n:08d}"
    user_id, sub_id = await _sub(container, token, tg=7700000 + n, **kw)
    return _h(token), sub_id, user_id


async def _post(
    http: httpx.AsyncClient, h: dict[str, str], body: Any = None, **kw: Any
) -> httpx.Response:
    if "content" not in kw:
        kw["content"] = json.dumps(_report() if body is None else body)
    headers = {"Content-Type": "application/json", **FWD, **h, **kw.pop("headers", {})}
    return await http.post(BASE, headers=headers, **kw)


async def _rows(container: ApiTestContainer) -> list[DevversionReport]:
    async with container.uow() as uow:
        return sorted(await uow.devversion_reports.list(), key=lambda r: r.id)


def _reset_limits(container: ApiTestContainer) -> None:
    """The fake Redis doesn't model expiry: drop every counter (per minute / hour / day)."""
    for key in [k for k in container.redis.store if k.startswith(("rl:", "dv:"))]:
        del container.redis.store[key]


async def _settings(container: ApiTestContainer, **values: Any) -> None:
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, values)
        await uow.commit()


async def _staff(
    http: httpx.AsyncClient, container: ApiTestContainer, role: Role
) -> dict[str, str]:
    name = f"staff_{role.name.lower()}"
    async with container.uow() as uow:
        await uow.users.add(
            User(
                username=name,
                auth_type=AuthType.EMAIL,
                role=role,
                referral_code=generate_referral_code(),
                password_hash=hash_password("StaffPass123!"),
            )
        )
        await uow.commit()
    res = await http.post(
        "/api/admin/auth/login", json={"username": name, "password": "StaffPass123!"}
    )
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


async def _audit_actions(container: ApiTestContainer) -> list[str]:
    async with container.uow() as uow:
        return [a.action for a in await uow.audit.list()]


# --- config, auth, gates -------------------------------------------------------------------
async def test_config_and_auth(client: Client) -> None:
    http, container = client
    h, _, _ = await _player(container, 1)
    res = await http.get(f"{BASE}/config", headers=h)
    assert res.status_code == 200
    assert res.json() == {
        "enabled": True,
        "reason": None,
        "consent_version": "1",
        "upload_interval_s": 3600,
        "max_report_bytes": 512 * 1024,
        "flags": {},
    }
    assert (await http.get(f"{BASE}/config")).status_code == 401
    assert (await _post(http, {"X-Subscription-Token": "WrongTok0000"})).status_code == 401


@pytest.mark.parametrize("expired_by", ["status", "date"])
async def test_inactive_subscription(client: Client, expired_by: str) -> None:
    http, container = client
    if expired_by == "status":
        h, _, _ = await _player(container, 2, status=SubscriptionStatus.EXPIRED)
    else:
        h, sub_id, _ = await _player(container, 2)
        async with container.uow() as uow:
            sub = await uow.subscriptions.get(sub_id)
            assert sub is not None
            sub.expire_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
            await uow.commit()
    for res in [await http.get(f"{BASE}/config", headers=h), await _post(http, h)]:
        assert (res.status_code, res.json()) == (403, {"detail": "subscription_inactive"})
    # wiping what was sent before stays possible
    res = await http.delete(BASE, headers=h)
    assert (res.status_code, res.json()) == (200, {"deleted": 0})


@pytest.mark.parametrize("trial_by", ["status", "flag"])
async def test_trial_subscriptions_are_refused(client: Client, trial_by: str) -> None:
    http, container = client
    if trial_by == "status":
        h, _, _ = await _player(container, 3, status=SubscriptionStatus.TRIAL)
    else:
        h, sub_id, _ = await _player(container, 3)
        async with container.uow() as uow:
            sub = await uow.subscriptions.get(sub_id)
            assert sub is not None
            sub.is_trial = True
            await uow.commit()
    cfg = (await http.get(f"{BASE}/config", headers=h)).json()
    assert (cfg["enabled"], cfg["reason"]) == (False, "trial_not_allowed")
    res = await _post(http, h)
    assert (res.status_code, res.json()) == (403, {"detail": "trial_not_allowed"})
    assert await _rows(container) == []


async def test_allow_list(client: Client) -> None:
    http, container = client
    listed, listed_id, _ = await _player(container, 4)
    other, _, _ = await _player(container, 5)
    await _settings(
        container, DEVVERSION_ALLOWED_SUBSCRIPTIONS=str(listed_id), DEVVERSION_FLAGS='{"x": true}'
    )
    cfg = (await http.get(f"{BASE}/config", headers=other)).json()
    assert (cfg["enabled"], cfg["reason"], cfg["flags"]) == (False, "not_allowed", {})
    res = await _post(http, other)
    assert (res.status_code, res.json()) == (403, {"detail": "not_allowed"})
    cfg = (await http.get(f"{BASE}/config", headers=listed)).json()
    assert (cfg["enabled"], cfg["flags"]) == (True, {"x": True})
    assert (await _post(http, listed)).status_code == 202
    # an empty list lets every paid subscription in again
    await _settings(container, DEVVERSION_ALLOWED_SUBSCRIPTIONS="")
    assert (await _post(http, other)).status_code == 202


async def test_kill_switch(client: Client) -> None:
    http, container = client
    h, _, _ = await _player(container, 6)
    assert (await _post(http, h)).status_code == 202
    await _settings(container, DEVVERSION_TELEMETRY_ENABLED=False, DEVVERSION_UPLOAD_INTERVAL=900)
    cfg = (await http.get(f"{BASE}/config", headers=h)).json()
    assert (cfg["enabled"], cfg["reason"], cfg["upload_interval_s"]) == (False, "disabled", 900)
    res = await _post(http, h)
    assert (res.status_code, res.json()) == (404, {"detail": "telemetry_disabled"})
    assert len(await _rows(container)) == 1
    assert (await http.delete(BASE, headers=h)).json() == {"deleted": 1}


# --- upload ---------------------------------------------------------------------------------
async def test_upload_plain_and_gzip_ip_not_stored(
    client: Client, _geo: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    logged: list[str] = []

    class _Log:
        def __getattr__(self, level: str) -> Any:
            return lambda *a, **kw: logged.append(repr((level, a, kw)))

    monkeypatch.setattr(app_telemetry, "log", _Log())
    h, sub_id, _ = await _player(container, 7)
    body = _report()
    res = await _post(http, h, body)
    assert (res.status_code, res.json()) == (202, {"ok": True})
    raw = gzip.compress(json.dumps(body).encode())
    res = await _post(http, h, content=raw, headers={"Content-Encoding": "gzip"})
    assert (res.status_code, res.json()) == (202, {"ok": True})
    assert _geo == [CLIENT_IP, CLIENT_IP]  # the real client (proxy header from a trusted peer)

    rows = await _rows(container)
    assert len(rows) == 2
    row = rows[0]
    assert (row.subscription_id, row.install_id, row.app_version) == (
        sub_id,
        body["install_id"],
        "0.5.0",
    )
    assert (row.asn, row.as_org, row.country, row.region) == (
        8402,
        "PJSC Vimpelcom",
        "RU",
        "Москва",
    )
    assert row.payload == body
    assert row.size == len(json.dumps(body).encode())
    assert row.digest["direct"] and row.digest["links"]
    # the address is nowhere: not in a column, the payload, the digest, the log or Redis
    stored = json.dumps(
        {c.key: getattr(row, c.key) for c in DevversionReport.__mapper__.column_attrs},
        default=str,
        ensure_ascii=False,
    )
    assert CLIENT_IP not in stored
    assert logged and not any(CLIENT_IP in line for line in logged)
    assert not any(CLIENT_IP in k or CLIENT_IP in v for k, v in container.redis.store.items())
    assert f"dv:ip:{ip_key(container, CLIENT_IP)}" in container.redis.store  # type: ignore[arg-type]


async def test_size_caps_and_gzip_bomb(client: Client) -> None:
    http, container = client
    h, _, _ = await _player(container, 8)
    big = json.dumps(_report(events=["x" * 30_000] * 20)).encode()
    assert len(big) > 512 * 1024
    res = await _post(http, h, content=big)  # declared Content-Length over the cap
    assert res.status_code == 413

    async def chunked() -> AsyncIterator[bytes]:  # no Content-Length: cut while streaming
        for i in range(0, len(big), 65536):
            yield big[i : i + 65536]

    res = await _post(http, h, content=chunked())
    assert res.status_code == 413
    _reset_limits(container)
    bomb = gzip.compress(b'{"schema": 1, "pad": "' + b"0" * (20 * 1024 * 1024) + b'"}')
    assert len(bomb) < 64 * 1024
    res = await _post(http, h, content=bomb, headers={"Content-Encoding": "gzip"})
    assert (res.status_code, res.json()) == (413, {"detail": "report_too_large"})
    assert await _rows(container) == []


@pytest.mark.parametrize(
    ("kwargs", "status", "detail"),
    [
        ({"body": _report(schema=2)}, 422, "unsupported_schema"),
        ({"body": _report(consent_version="2")}, 422, "consent_version"),
        ({"body": _report(install_id="123")}, 422, "install_id"),
        ({"body": _report(app_version="x" * 40)}, 422, "app_version"),
        ({"body": _report(apps={"a": 1})}, 422, "apps_not_list"),
        ({"body": _report(system="Windows")}, 422, "system_not_object"),
        ({"content": b'{"schema": NaN}'}, 400, "invalid_json"),
        ({"content": b"not gzip", "headers": {"Content-Encoding": "gzip"}}, 400, "bad_gzip"),
        ({"content": b"{}", "headers": {"Content-Encoding": "br"}}, 415, "unsupported_encoding"),
        (
            {"content": b"{}", "headers": {"Content-Type": "text/plain"}},
            415,
            "unsupported_media_type",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "case",
)
async def test_validation(client: Client, kwargs: dict[str, Any], status: int, detail: str) -> None:
    http, container = client
    h, _, _ = await _player(container, 9)
    kwargs = dict(kwargs)
    res = await _post(http, h, kwargs.pop("body", None), **kwargs)
    assert (res.status_code, res.json()) == (status, {"detail": detail})
    assert await _rows(container) == []


# --- disk guards ------------------------------------------------------------------------------
async def test_count_limits_per_subscription_and_ip(client: Client) -> None:
    http, container = client
    h, sub_id, _ = await _player(container, 10)
    for _ in range(6):
        assert (await _post(http, h)).status_code == 202
    assert (await _post(http, h)).status_code == 429  # 7th within the hour
    _reset_limits(container)
    container.redis.store[f"dv:day:{sub_id}"] = "60"  # the day's quota is used up
    assert (await _post(http, h)).status_code == 429
    # 100 a day from one IP, whatever the subscription
    _reset_limits(container)
    other, _, _ = await _player(container, 11)
    ip = ip_key(container, CLIENT_IP)  # type: ignore[arg-type]
    container.redis.store[f"dv:ip:{ip}"] = "100"
    assert (await _post(http, other)).status_code == 429
    elsewhere = await _post(http, other, headers={"X-Forwarded-For": "95.24.99.1"})
    assert elsewhere.status_code == 202
    assert len(await _rows(container)) == 7


async def test_byte_quotas_per_account_and_overall(client: Client) -> None:
    http, container = client
    h, _, user_id = await _player(container, 12)
    size = len(json.dumps(_report()).encode())
    # the account's 20 MB a day: what's left is less than one report → 429, counter unchanged
    user_key = f"dv:bytes:user:{user_id}"
    container.redis.store[user_key] = str(20 * 1024 * 1024 - size + 1)
    assert (await _post(http, h)).status_code == 429
    assert container.redis.store[user_key] == str(20 * 1024 * 1024 - size + 1)
    # exactly at the budget still goes through
    container.redis.store[user_key] = str(20 * 1024 * 1024 - size)
    assert (await _post(http, h)).status_code == 202
    assert int(container.redis.store[user_key]) == 20 * 1024 * 1024
    # everyone's 1 GB a day
    _reset_limits(container)
    container.redis.store["dv:bytes:all"] = str(1024**3)
    assert (await _post(http, h)).status_code == 429  # refused before the body is read
    container.redis.store["dv:bytes:all"] = str(1024**3 - 10)
    assert (await _post(http, h)).status_code == 429
    assert container.redis.store["dv:bytes:all"] == str(1024**3 - 10)  # given back
    assert len(await _rows(container)) == 1


async def test_redis_down_is_503(client: Client) -> None:
    http, container = client
    h, _, _ = await _player(container, 13)

    class _Down:
        def __getattr__(self, name: str) -> Any:
            def fail(*a: Any, **kw: Any) -> Any:
                raise ConnectionError("redis down")

            return fail

    container.redis = _Down()  # type: ignore[assignment]
    res = await _post(http, h)
    assert (res.status_code, res.json()) == (503, {"detail": "temporarily_unavailable"})
    assert await _rows(container) == []


async def test_keeps_only_the_newest_reports_per_subscription(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    monkeypatch.setattr(app_telemetry, "MAX_REPORTS_PER_SUBSCRIPTION", 3)
    h, sub_id, _ = await _player(container, 14)
    o, other_id, _ = await _player(container, 15)
    assert (await _post(http, o)).status_code == 202
    for _ in range(5):
        assert (await _post(http, h)).status_code == 202
    rows = await _rows(container)
    mine = [r.id for r in rows if r.subscription_id == sub_id]
    assert len(mine) == 3 and mine == sorted(mine)[-3:]
    assert [r.subscription_id for r in rows].count(other_id) == 1  # others untouched


async def test_delete_removes_only_own_reports(client: Client) -> None:
    http, container = client
    a, _, _ = await _player(container, 16)
    b, sub_b, _ = await _player(container, 17)
    for h in (a, a, b):
        assert (await _post(http, h)).status_code == 202
    res = await http.delete(BASE, headers=a)
    assert (res.status_code, res.json()) == (200, {"deleted": 2})
    assert [r.subscription_id for r in await _rows(container)] == [sub_b]
    assert (await http.delete(BASE, headers=a)).json() == {"deleted": 0}


async def test_retention_and_size_alert(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    http, container = client
    h, _, _ = await _player(container, 18)
    for _ in range(3):
        assert (await _post(http, h)).status_code == 202
    rows = await _rows(container)
    async with container.uow() as uow:
        for row, days in zip(rows, (91, 89, 0), strict=True):
            stored = await uow.devversion_reports.get(row.id)
            assert stored is not None
            stored.received_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        await uow.commit()
    alerts: list[str] = []

    async def notify(text: str, *, topic: str | None = None) -> None:
        alerts.append(f"{topic}:{text}")

    monkeypatch.setattr(container.notifier, "notify_admins", notify)
    monkeypatch.setattr(tasks, "get_container", lambda: container)
    assert await tasks.devversion_retention() == 1
    assert sorted(r.id for r in await _rows(container)) == sorted(r.id for r in rows[1:])
    assert alerts == []  # sqlite: no size to measure

    async def huge(self: DevversionReportDAO) -> int:
        return 4 * 1024**3

    monkeypatch.setattr(DevversionReportDAO, "table_bytes", huge)
    await tasks.devversion_retention()
    await tasks.devversion_retention()  # once a day, not on every run
    assert len(alerts) == 1 and alerts[0].startswith("alerts:") and "4.0 ГБ" in alerts[0]


# --- admin --------------------------------------------------------------------------------------
async def test_admin_section(client: Client) -> None:
    http, container = client
    auth = await _login(http)
    h, sub_id, _ = await _player(container, 19, username="tester", email="t@example.com")
    o, other_id, _ = await _player(container, 20)
    for hh in (h, h, o):
        assert (await _post(http, hh)).status_code == 202

    cfg = (await http.get(f"{ADMIN}/settings", headers=auth)).json()
    assert (cfg["enabled"], cfg["upload_interval_s"], cfg["flags"]) == (True, 3600, {})
    assert (cfg["can_edit"], cfg["can_see_raw"], cfg["can_export"]) == (True, True, True)
    assert cfg["geoip"] == {"asn": None, "city": None}
    res = await http.put(
        f"{ADMIN}/settings",
        headers=auth,
        json={
            "enabled": False,
            "upload_interval_s": 1800,
            "flags": {"new_probe": True},
            "allowed_subscriptions": f"{sub_id}, {other_id}",
        },
    )
    assert res.status_code == 200, res.text
    saved = res.json()
    assert (saved["enabled"], saved["flags"], saved["allowed_subscriptions"]) == (
        False,
        {"new_probe": True},
        sorted([sub_id, other_id]),
    )
    app_cfg = (await http.get(f"{BASE}/config", headers=h)).json()
    assert (app_cfg["enabled"], app_cfg["upload_interval_s"]) == (False, 1800)
    for bad in (
        {"flags": "[1]"},
        {"flags": {"Bad-Key": 1}},
        {"flags": {"x": {"y": 1}}},
        {"upload_interval_s": 10},
        {"allowed_subscriptions": "1, x"},
        {},
    ):
        res = await http.put(f"{ADMIN}/settings", headers=auth, json=bad)
        assert res.status_code == 400, bad
    # the generic settings screen applies the same checks
    res = await http.patch(
        "/api/admin/settings", headers=auth, json={"changes": {"DEVVERSION_FLAGS": "nope"}}
    )
    assert res.status_code == 400

    page = (await http.get(f"{ADMIN}/reports", headers=auth)).json()
    assert page["total"] == 3
    item = page["items"][-1]
    assert item["subscription_id"] == sub_id and item["subscription_label"] == "@tester"
    assert (item["asn"], item["mode"], item["counts"]["nodes"]) == (8402, "tun", 1)
    assert item["size"] > 0
    assert "payload" not in item and "t@example.com" not in json.dumps(page)
    page = (
        await http.get(
            f"{ADMIN}/reports",
            headers=auth,
            params={"subscription_id": other_id, "asn": 8402, "app_version": "0.5.0"},
        )
    ).json()
    assert page["total"] == 1
    for params in ({"asn": 0}, {"app_version": "9.9"}, {"date_to": "2020-01-01"}):
        r = await http.get(f"{ADMIN}/reports", headers=auth, params=params)
        assert r.json()["total"] == 0, params
    today = dt.datetime.now(dt.UTC).date().isoformat()
    r = await http.get(
        f"{ADMIN}/reports", headers=auth, params={"date_from": today, "date_to": today}
    )
    assert r.json()["total"] == 3

    one = (await http.get(f"{ADMIN}/reports/{item['id']}", headers=auth)).json()
    assert one["payload"]["schema"] == 1
    assert (await http.get(f"{ADMIN}/reports/999", headers=auth)).status_code == 404

    facets = (await http.get(f"{ADMIN}/facets", headers=auth)).json()
    assert facets["providers"] == [{"asn": 8402, "as_org": "PJSC Vimpelcom", "reports": 3}]
    summary = (await http.get(f"{ADMIN}/summary", headers=auth)).json()
    assert summary["reports"] == 3 and summary["truncated"] is False
    link = summary["links"][0]
    assert (link["samples"], link["subscriptions"], link["latency_median"]) == (3, 2, 42.0)
    assert {r["name"] for r in summary["direct_failures"]} == {"Discord", "youtube.com"}
    assert all(r["subscriptions"] == 2 for r in summary["direct_failures"])
    assert summary["games"][0]["game"] == "PUBG"

    res = await http.get(f"{ADMIN}/export", headers=auth, params={"asn": 8402})
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    exported = res.json()
    assert len(exported["reports"]) == 3 and exported["reports"][0]["payload"]
    assert exported["truncated"] is False and "DB-IP" in exported["attribution"]
    assert {"devversion.view", "devversion.export", "devversion.settings"} <= set(
        await _audit_actions(container)
    )

    res = await http.delete(f"{ADMIN}/subscriptions/{sub_id}/reports", headers=auth)
    assert res.json() == {"deleted": 2}
    assert [r.subscription_id for r in await _rows(container)] == [other_id]


async def test_raw_payload_only_for_owner_dev_and_the_key(client: Client) -> None:
    http, container = client
    h, _, _ = await _player(container, 21)
    assert (await _post(http, h)).status_code == 202
    report_id = (await _rows(container))[0].id
    admin = await _staff(http, container, Role.ADMIN)
    dev = await _staff(http, container, Role.DEV)

    one = (await http.get(f"{ADMIN}/reports/{report_id}", headers=admin)).json()
    assert one["payload"] == "***" and one["app_version"] == "0.5.0"
    assert (await http.get(f"{ADMIN}/reports", headers=admin)).json()["total"] == 1
    assert (await http.get(f"{ADMIN}/summary", headers=admin)).status_code == 200
    cfg = (await http.get(f"{ADMIN}/settings", headers=admin)).json()
    assert (cfg["can_edit"], cfg["can_see_raw"], cfg["can_export"]) == (False, False, False)
    assert (await http.get(f"{ADMIN}/export", headers=admin)).status_code == 403
    # settings: OWNER / DEV only, here and on the generic settings screen
    res = await http.put(f"{ADMIN}/settings", headers=admin, json={"enabled": False})
    assert res.status_code == 403
    for res in (
        await http.patch(
            "/api/admin/settings", headers=admin, json={"changes": {"DEVVERSION_FLAGS": "{}"}}
        ),
        await http.post(
            "/api/admin/settings/reset", headers=admin, json={"keys": ["DEVVERSION_FLAGS"]}
        ),
    ):
        assert res.status_code == 403

    one = (await http.get(f"{ADMIN}/reports/{report_id}", headers=dev)).json()
    assert one["payload"]["schema"] == 1
    assert (await http.get(f"{ADMIN}/export", headers=dev)).status_code == 200
    res = await http.put(f"{ADMIN}/settings", headers=dev, json={"upload_interval_s": 1200})
    assert res.status_code == 200
    assert (await _audit_actions(container)).count("devversion.view") == 2


async def test_admin_read_only_key(client: Client) -> None:
    http, container = client
    owner = await _login(http)
    h, sub_id, _ = await _player(container, 22)
    assert (await _post(http, h)).status_code == 202
    key, _ = await _issue(http, owner, allowed_screens=["devversion"])
    auth = _bearer(key)
    report_id = (await _rows(container))[0].id
    for path in (
        f"{ADMIN}/settings",
        f"{ADMIN}/facets",
        f"{ADMIN}/reports",
        f"{ADMIN}/summary",
    ):
        res = await http.get(path, headers=auth)
        assert res.status_code == 200, (path, res.text)
    # the key reads a whole report (that's what it's for) — and it's audited
    one = await http.get(f"{ADMIN}/reports/{report_id}", headers=auth)
    assert one.status_code == 200 and one.json()["payload"]["schema"] == 1
    assert "devversion.view" in await _audit_actions(container)
    # ...but never the bulk export
    res = await http.get(f"{ADMIN}/export", headers=auth)
    assert (res.status_code, res.json()["detail"]) == (403, "этот раздел закрыт для ключей")
    res = await http.put(f"{ADMIN}/settings", headers=auth, json={"enabled": False})
    assert (res.status_code, res.json()["detail"]) == (403, "ключ только для чтения")
    res = await http.delete(f"{ADMIN}/subscriptions/{sub_id}/reports", headers=auth)
    assert res.status_code == 403
    assert len(await _rows(container)) == 1
    assert (await http.get(f"{ADMIN}/settings", headers=owner)).json()["enabled"]


# --- counters, hard stop, summary limits, staff without raw access ------------------------------
async def test_counters_always_get_a_ttl_and_give_back_never_goes_negative(
    client: Client, _redis: _DvRedis
) -> None:
    http, container = client
    h, sub_id, _ = await _player(container, 30)
    # a counter left without a TTL (EXPIRE failed after INCRBY once) is healed by the next call
    _redis.store[f"dv:day:{sub_id}"] = "3"
    assert (await _post(http, h)).status_code == 202
    assert _redis.ttls[f"dv:day:{sub_id}"] == 86400
    assert _redis.store[f"dv:day:{sub_id}"] == "4"
    assert all(k in _redis.ttls for k in _redis.store if k.startswith("dv:"))
    # giving bytes back to an expired window doesn't recreate it (and never goes below zero)
    await app_telemetry._take_back(container, "dv:bytes:user:gone", 100)  # type: ignore[arg-type]
    assert "dv:bytes:user:gone" not in _redis.store
    _redis.store["dv:bytes:user:low"] = "40"
    await app_telemetry._take_back(container, "dv:bytes:user:low", 100)  # type: ignore[arg-type]
    assert _redis.store["dv:bytes:user:low"] == "0"


async def test_storage_full_stops_uploads_before_the_body(
    client: Client, _redis: _DvRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    h, _, _ = await _player(container, 31)
    monkeypatch.setattr(tasks, "get_container", lambda: container)

    async def size(self: DevversionReportDAO) -> int:
        return sizes[-1]

    sizes = [4 * 1024**3]
    monkeypatch.setattr(DevversionReportDAO, "table_bytes", size)
    await tasks.devversion_retention()
    assert _redis.store.get(STORAGE_FULL_KEY) == "1"
    # 503 before anything is read: even a body that would be refused (415) isn't looked at
    res = await _post(http, h, content=b"x", headers={"Content-Type": "text/plain"})
    assert (res.status_code, res.json()) == (503, {"detail": "storage_full"})
    assert not any(k.startswith("dv:hour:") for k in _redis.store)  # no quota spent either
    # back under the ceiling → the next retention run lifts the stop
    sizes.append(1024**3)
    await tasks.devversion_retention()
    assert STORAGE_FULL_KEY not in _redis.store
    assert (await _post(http, h)).status_code == 202


async def test_summary_stops_at_its_report_ceiling(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    auth = await _login(http)
    h, _, _ = await _player(container, 32)
    for _ in range(4):
        assert (await _post(http, h)).status_code == 202
    monkeypatch.setattr(admin_devversion, "_SUMMARY_REPORTS", 3)
    out = (await http.get(f"{ADMIN}/summary", headers=auth)).json()
    assert (out["reports"], out["truncated"]) == (3, True)
    monkeypatch.setattr(admin_devversion, "_SUMMARY_REPORTS", 1000)
    monkeypatch.setattr(admin_devversion, "SUMMARY_CELLS", 10)  # each report has 4 cells
    out = (await http.get(f"{ADMIN}/summary", headers=auth)).json()
    assert (out["reports"], out["truncated"]) == (2, True)
    monkeypatch.setattr(admin_devversion, "SUMMARY_CELLS", 200_000)
    out = (await http.get(f"{ADMIN}/summary", headers=auth)).json()
    assert (out["reports"], out["truncated"]) == (4, False)
    assert out["links"][0]["single_source"] is True  # all four came from one subscription


async def test_staff_without_raw_access_sees_no_place_and_no_per_subscription_summary(
    client: Client,
) -> None:
    http, container = client
    owner = await _login(http)
    h, sub_id, _ = await _player(container, 33)
    assert (await _post(http, h)).status_code == 202
    admin = await _staff(http, container, Role.ADMIN)

    item = (await http.get(f"{ADMIN}/reports", headers=admin)).json()["items"][0]
    assert (item["country"], item["as_org"], item["region"], item["city"]) == (
        "RU",
        "PJSC Vimpelcom",
        None,
        None,
    )
    one = (await http.get(f"{ADMIN}/reports/{item['id']}", headers=admin)).json()
    assert (one["region"], one["city"], one["payload"]) == (None, None, "***")
    full = (await http.get(f"{ADMIN}/reports", headers=owner)).json()["items"][0]
    assert (full["region"], full["city"]) == ("Москва", "Москва")

    res = await http.get(f"{ADMIN}/summary", headers=admin, params={"subscription_id": sub_id})
    assert res.status_code == 403
    assert (await http.get(f"{ADMIN}/summary", headers=admin)).status_code == 200
    res = await http.get(f"{ADMIN}/summary", headers=owner, params={"subscription_id": sub_id})
    assert res.status_code == 200 and res.json()["reports"] == 1
    assert (await _audit_actions(container)).count("devversion.summary") == 1
