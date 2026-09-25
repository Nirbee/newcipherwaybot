"""Router agent API: bearer-token auth, config ETag/304, heartbeat, install-report.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import dataclasses

import httpx

from src.application.dto.panel import PanelHost
from src.application.dto.pricing import PurchaseRequest
from src.core.enums import Currency, RouterDeviceStatus, SubscriptionStatus
from tests.factories import make_plan, make_user
from tests.integration.test_admin_api import ApiTestContainer, _login, client  # noqa: F401


def _host(uuid: str, **overrides: object) -> PanelHost:
    base: dict[str, object] = {
        "uuid": uuid,
        "remark": f"Host {uuid}",
        "address": "203.0.113.1",
        "port": 443,
        "protocol": "vless",
        "network": "tcp",
        "security": "reality",
        "sni": "example.com",
        "fingerprint": "chrome",
        "public_key": "c-i5ggNipCtGIyqBVjcbctf3jLltOcsuLwS1q5RQanI",
        "short_id": "abcd1234",
        "path": None,
        "xhttp_mode": None,
        "service_name": None,
        "is_disabled": False,
        "squad_uuids": (),
    }
    base.update(overrides)
    return PanelHost(**base)  # type: ignore[arg-type]


async def _create_device(
    http: httpx.AsyncClient,
    container: ApiTestContainer,
    *,
    telegram_id: int = 42,
    vless_uuid: str = "test-vless-uuid",
    host_uuids: list[str] | None = None,
) -> tuple[int, str]:
    """Grants a subscription, tags its fake panel user with a known vless_uuid, wires it to
    the given hosts, and creates a RouterDevice for it via the admin API. Returns
    (device_id, plain_token)."""
    async with container.uow() as uow:
        user = await make_user(uow, telegram_id=telegram_id)
        plan, _ = await make_plan(uow, code=f"plan-{telegram_id}")
        await uow.commit()
        req = PurchaseRequest(
            user_id=user.id, plan_id=plan.id, duration_days=30, currency=Currency.RUB
        )
        sub = await container.subscriptions.grant(uow, user=user, plan=plan, req=req)
        user.current_subscription_id = sub.id
        await uow.commit()
        sub_id = sub.id
        panel_uuid = sub.remnawave_uuid

    auth = await _login(http)
    if host_uuids:
        await http.put(
            "/api/admin/routers/config/eligible-hosts",
            headers=auth,
            json={"host_uuids": host_uuids},
        )
        body: dict[str, object] = {"subscription_id": sub_id, "label": f"router-{telegram_id}"}
    else:
        body = {"subscription_id": sub_id, "label": f"router-{telegram_id}"}
    res = await http.post("/api/admin/routers", headers=auth, json=body)
    assert res.status_code == 200, res.text
    created = res.json()

    # AFTER device creation: creating a device best-effort tags the panel user, which the fake
    # client implements by rebuilding the whole PanelUser from the ProvisionSpec — losing any
    # field the spec doesn't carry (vless_uuid isn't one of them). Setting it after matches
    # real Remnawave's behavior anyway (vless_uuid exists from original provisioning, untouched
    # by a later partial PATCH — only this simplistic fake conflates "update" with "replace").
    existing = container.remnawave_client.users[panel_uuid]
    container.remnawave_client.users[panel_uuid] = dataclasses.replace(
        existing, vless_uuid=vless_uuid
    )

    return created["id"], created["token"]


async def test_config_requires_bearer_token(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, _ = client
    res = await http.get("/api/agent/config")
    assert res.status_code == 401


async def test_config_rejects_unknown_token(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, _ = client
    res = await http.get("/api/agent/config", headers={"Authorization": "Bearer nope"})
    assert res.status_code == 401


async def test_config_rejects_revoked_device(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=1)
    auth = await _login(http)
    await http.post(f"/api/admin/routers/{device_id}/revoke", headers=auth)

    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


async def test_config_builds_proxy_outbound_with_assigned_host(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    device_id, token = await _create_device(
        http, container, telegram_id=2, vless_uuid="vless-uuid-2", host_uuids=["host-a"]
    )

    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert "ETag" in res.headers
    body = res.json()
    proxy_tags = [o["tag"] for o in body["outbounds"] if o["tag"].startswith("proxy-")]
    assert len(proxy_tags) == 1
    assert body["outbounds"][0]["settings"]["vnext"][0]["users"][0]["id"] == "vless-uuid-2"


async def test_config_etag_returns_304_on_match(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    device_id, token = await _create_device(
        http, container, telegram_id=3, host_uuids=["host-a"]
    )
    headers = {"Authorization": f"Bearer {token}"}

    first = await http.get("/api/agent/config", headers=headers)
    assert first.status_code == 200
    etag = first.headers["ETag"]

    # The fake redis has no real TTL — clear the rate-limit key to simulate the (real) 50s
    # window having elapsed, so this test isolates ETag/304 behavior from rate limiting.
    container.redis.store.pop(f"agent:cfg:{device_id}", None)

    second = await http.get(
        "/api/agent/config", headers={**headers, "If-None-Match": etag}
    )
    assert second.status_code == 304


async def test_config_inactive_subscription_is_freedom_only(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    device_id, token = await _create_device(
        http, container, telegram_id=4, host_uuids=["host-a"]
    )
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        sub = await uow.subscriptions.get(device.subscription_id)
        sub.status = SubscriptionStatus.EXPIRED
        await uow.commit()

    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    tags = [o["tag"] for o in res.json()["outbounds"]]
    assert tags == ["direct", "block"]


async def test_heartbeat_updates_device_status(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=5)
    res = await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"xray_version": "25.1.30", "xray_running": True, "external_ip": "1.2.3.4"},
    )
    assert res.status_code == 204
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
    assert device.status is RouterDeviceStatus.ONLINE
    assert device.xray_version == "25.1.30"
    assert device.external_ip == "1.2.3.4"
    assert device.last_seen_at is not None


async def test_heartbeat_xray_not_running_marks_offline(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=6)
    res = await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"xray_running": False, "last_error": "config test failed"},
    )
    assert res.status_code == 204
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
    assert device.status is RouterDeviceStatus.OFFLINE
    assert device.last_error == "config test failed"


async def test_install_report_stores_body_and_flips_pending_to_online(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=7)
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        assert device.status is RouterDeviceStatus.PENDING

    report = {"fs_type": "ext4", "netfilter": True, "xray_version": "25.1.30", "tunnel_ok": True}
    res = await http.post(
        "/api/agent/install-report",
        headers={"Authorization": f"Bearer {token}"},
        json=report,
    )
    assert res.status_code == 204
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
    assert device.install_report == report
    assert device.status is RouterDeviceStatus.ONLINE


async def test_heartbeat_rate_limited_on_rapid_repeat(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=8)
    headers = {"Authorization": f"Bearer {token}"}
    first = await http.post("/api/agent/heartbeat", headers=headers, json={})
    assert first.status_code == 204
    second = await http.post("/api/agent/heartbeat", headers=headers, json={})
    assert second.status_code == 429


_HAPP_JSON = [
    {
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "domain": ["domain:gosuslugi.ru"], "outboundTag": "direct"},
                {"type": "field", "network": "tcp,udp", "balancerTag": "auto"},
            ],
            "balancers": [{"tag": "auto", "selector": ["proxy"], "fallbackTag": "direct"}],
        },
    }
]


async def test_config_includes_split_tunnel_rules_from_happ_subscription(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    container.remnawave_client.subscription_json = _HAPP_JSON
    device_id, token = await _create_device(
        http, container, telegram_id=20, host_uuids=["host-a"]
    )

    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    rules = res.json()["routing"]["rules"]
    assert rules[0] == {
        "type": "field",
        "domain": ["domain:gosuslugi.ru"],
        "outboundTag": "direct",
    }
    assert rules[-1]["balancerTag"] == "balancer"
    assert res.json()["routing"]["balancers"][0]["fallbackTag"] == "direct"

    # Cached: the next poll doesn't hit the subscription page again.
    container.redis.store.pop(f"agent:cfg:{device_id}", None)
    await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert container.remnawave_client.subscription_json_fetches == 1


async def test_config_falls_back_to_last_good_rules_when_fetch_fails(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    container.remnawave_client.subscription_json = _HAPP_JSON
    device_id, token = await _create_device(
        http, container, telegram_id=21, host_uuids=["host-a"]
    )
    headers = {"Authorization": f"Bearer {token}"}
    first = await http.get("/api/agent/config", headers=headers)

    store = container.redis.store
    for key in [k for k in store if k.startswith("agent:rt:") and not k.endswith(":last")]:
        store.pop(key)
    container.redis.store.pop(f"agent:cfg:{device_id}", None)
    container.remnawave_client.subscription_json = RuntimeError("sub page down")

    second = await http.get("/api/agent/config", headers=headers)
    assert second.status_code == 200
    assert second.headers["ETag"] == first.headers["ETag"]


async def test_heartbeat_stores_diagnostics_shown_in_admin_detail(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=30)
    diag = {"agent_version": "2", "route_only": "false", "ports_proxied": "", "cron": "ok"}
    res = await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"xray_running": True, "diagnostics": diag},
    )
    assert res.status_code == 204

    detail = await http.get(f"/api/admin/routers/{device_id}", headers=await _login(http))
    assert detail.json()["diagnostics"] == diag


async def test_heartbeat_ignores_oversized_diagnostics(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=31)
    await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"xray_running": True, "diagnostics": {"blob": "x" * 50_000}},
    )
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
    assert device.diagnostics is None


async def test_whoami_names_router_and_client(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=32)
    res = await http.get("/api/agent/whoami", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == device_id
    assert body["label"] == "router-32"
    assert body["client"]

    bad = await http.get("/api/agent/whoami", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


async def test_scripts_are_served_and_config_advertises_agent_version(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    agent = await http.get("/api/agent/agent.sh")
    installer = await http.get("/api/agent/install.sh")
    assert agent.status_code == 200 and agent.text.startswith("#!/bin/sh")
    assert installer.status_code == 200 and "cipherway-agent" in installer.text
    version = agent.text.split('AGENT_VERSION="', 1)[1].split('"', 1)[0]

    container.remnawave_client.hosts = [_host("host-a")]
    _, token = await _create_device(http, container, telegram_id=33, host_uuids=["host-a"])
    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.headers["X-Agent-Version"] == version
