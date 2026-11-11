"""Router agent API: bearer-token auth, config ETag/304, heartbeat, install-report.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import dataclasses
import json

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
    device_id, token = await _create_device(http, container, telegram_id=3, host_uuids=["host-a"])
    headers = {"Authorization": f"Bearer {token}"}

    first = await http.get("/api/agent/config", headers=headers)
    assert first.status_code == 200
    etag = first.headers["ETag"]

    # The fake redis has no real TTL — clear the rate-limit key to simulate the (real) 50s
    # window having elapsed, so this test isolates ETag/304 behavior from rate limiting.
    container.redis.store.pop(f"agent:cfg:{device_id}", None)

    second = await http.get("/api/agent/config", headers={**headers, "If-None-Match": etag})
    assert second.status_code == 304


async def test_config_inactive_subscription_is_freedom_only(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    device_id, token = await _create_device(http, container, telegram_id=4, host_uuids=["host-a"])
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
    device_id, token = await _create_device(http, container, telegram_id=20, host_uuids=["host-a"])

    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    rules = [r for r in res.json()["routing"]["rules"] if "inboundTag" not in r]
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
    device_id, token = await _create_device(http, container, telegram_id=21, host_uuids=["host-a"])
    headers = {"Authorization": f"Bearer {token}"}
    first = await http.get("/api/agent/config", headers=headers)

    store = container.redis.store
    for key in [k for k in store if k.startswith("agent:sub:") and not k.endswith(":last")]:
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
    await http.put(  # staged rollout: only listed routers are offered the new agent
        "/api/admin/routers/config/agent-rollout", headers=await _login(http), json={"all": True}
    )
    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.headers["X-Agent-Version"] == version


def _sub_server(remark: str, address: str) -> dict:
    return {
        "remarks": remark,
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [{"address": address, "port": 443, "users": [{"id": "cust-uuid"}]}]
                },
                "streamSettings": {"network": "xhttp", "security": "reality"},
            },
            {"tag": "direct", "protocol": "freedom"},
        ],
    }


async def test_config_takes_servers_from_subscription_and_flags_a_foreign_one(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """Field failure: the router was pinned to a server outside the customer's squads, so the
    node rejected the UUID and everything left via direct. Now the router gets the servers the
    customer's subscription really has, and the card says what was substituted."""
    http, container = client
    container.remnawave_client.hosts = [
        _host("host-de", remark="DE", address="203.0.113.1"),
        _host("host-nl", remark="NL", address="203.0.113.2"),  # in the panel, not assigned
    ]
    container.remnawave_client.subscription_json = [_sub_server("🇳🇱 NL", "203.0.113.2")]
    device_id, token = await _create_device(http, container, telegram_id=30, host_uuids=["host-de"])

    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    proxies = [o for o in res.json()["outbounds"] if o["tag"].startswith("proxy-")]
    assert [o["settings"]["vnext"][0]["address"] for o in proxies] == ["203.0.113.2"]
    assert proxies[0]["settings"]["vnext"][0]["users"][0]["id"] == "cust-uuid"

    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=await _login(http))).json()
    info = detail["config_info"]
    assert info["source"] == "subscription"
    assert info["servers"] == ["🇳🇱 NL"]
    assert "DE" in info["warning"]


async def test_app_not_supported_stub_server_is_never_used(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """Field failure: after the hosts were rebuilt, Remnawave's «App not supported» stub came
    with a dummy server; the router took it for a real one and every connection was reset.
    Stub servers are dropped and the assigned panel host is used instead."""
    http, container = client
    container.remnawave_client.hosts = [_host("host-de", remark="DE", address="203.0.113.1")]
    stub = _sub_server("App not supported", "0.0.0.0")
    container.remnawave_client.subscription_json = [stub]
    device_id, token = await _create_device(http, container, telegram_id=32, host_uuids=["host-de"])

    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    proxies = [o for o in res.json()["outbounds"] if o["tag"].startswith("proxy-")]
    assert [o["settings"]["vnext"][0]["address"] for o in proxies] == ["203.0.113.1"]

    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=await _login(http))).json()
    assert detail["config_info"]["source"] == "panel"
    assert detail["config_info"]["servers"] == ["DE"]


def test_stub_filter_keeps_real_servers() -> None:
    from src.application.services.router_config import SubscriptionProxy
    from src.web.routes.agent import real_subscription_proxies

    def sp(remark: str, address: str) -> SubscriptionProxy:
        return SubscriptionProxy(remark=remark, address=address, port=443, outbound={})

    hosts = [_host("host-de", address="203.0.113.1")]
    got = real_subscription_proxies(
        [sp("DE", "203.0.113.1"), sp("App not supported", "203.0.113.1"), sp("?", "0.0.0.0")],
        hosts,
    )
    assert [p.remark for p in got] == ["DE"]


def _selftest(balancer: dict, direct_ip: str = "91.0.0.1") -> dict:
    return {
        "self_test": [
            {"name": "direct", "ok": True, "ip": direct_ip, "big": "ok", "error": ""},
            {"name": "proxy-aaaaaaaa", "ok": True, "ip": "132.0.0.9", "big": "ok", "error": ""},
            {"name": "balancer", **balancer},
        ]
    }


def test_vpn_verdict_reads_the_agent_self_test() -> None:
    from src.web.routes.agent import vpn_verdict

    assert vpn_verdict({}) is None  # old agent: no verdict rather than a guess
    ok = vpn_verdict(_selftest({"ok": True, "ip": "132.0.0.9", "big": "ok"}))
    assert ok and ok["state"] == "ok" and "132.0.0.9" in ok["text"]
    # the field failure: probe got out, but with the router's own ISP address
    leak = vpn_verdict(_selftest({"ok": True, "ip": "91.0.0.1", "big": "ok"}))
    assert leak and leak["state"] == "direct" and "напрямую" in leak["text"]
    # the server passed on its own, so this is Xray's health check, not a dead server
    assert "серверы отвечают" in leak["text"]
    slow = vpn_verdict(
        _selftest({"ok": True, "ip": "132.0.0.9", "big": "оборвалось на 16384 байт"})
    )
    assert slow and slow["state"] == "slow"
    dead = vpn_verdict(_selftest({"ok": False, "ip": "", "error": "curl: (7) refused"}))
    assert dead and dead["state"] == "fail" and "refused" in dead["text"]


def test_vpn_verdict_without_the_direct_probe() -> None:
    """The direct probe failed, so there's no ISP address to compare with — a balancer that
    fell back to direct must still not read as "VPN works"."""
    from src.web.routes.agent import vpn_verdict

    via_vpn = {"ok": True, "ip": "132.0.0.9", "big": "ok"}
    leaked = {"ok": True, "ip": "91.0.0.1", "big": "ok"}
    # exit matches a server that passed on its own -> still a confirmed "ok"
    assert vpn_verdict(_selftest(via_vpn, direct_ip=""))["state"] == "ok"  # type: ignore[index]
    # the router's address from its heartbeat stands in for the failed direct probe
    v = vpn_verdict(_selftest(leaked, direct_ip=""), isp_ip="91.0.0.1")
    assert v and v["state"] == "direct" and "91.0.0.1" in v["text"]
    # every server failed while the balancer passed -> it can only be the direct fallback
    tests = _selftest(leaked, direct_ip="")
    tests["self_test"][1] = {"name": "proxy-aaaaaaaa", "ok": False, "ip": "", "error": "timeout"}
    assert vpn_verdict(tests)["state"] == "direct"  # type: ignore[index]
    # nothing to compare with at all -> "not verified", never a false "ok"
    v = vpn_verdict(_selftest(leaked, direct_ip=""))
    assert v and v["state"] == "unknown"


async def test_probe_endpoints_need_the_device_token(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    _device_id, token = await _create_device(http, container, telegram_id=31)
    assert (await http.get("/api/agent/ip")).status_code == 401
    headers = {"Authorization": f"Bearer {token}", "X-Forwarded-For": "132.0.0.9, 10.0.0.1"}
    assert (await http.get("/api/agent/ip", headers=headers)).text == "132.0.0.9"
    blob = await http.get("/api/agent/probe", headers=headers)
    assert blob.status_code == 200 and len(blob.content) == 256 * 1024


async def test_broken_vpn_alerts_once_after_two_reports_and_again_on_recovery(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=32)
    sent: list[str] = []

    async def fake_report(_container, code: str, text: str, **_kw) -> bool:
        sent.append(f"{code}:{text}")
        return True

    monkeypatch.setattr("src.infrastructure.services.reports.send_topic_report", fake_report)
    headers = {"Authorization": f"Bearer {token}"}
    leak = {"diagnostics": _selftest({"ok": True, "ip": "91.0.0.1", "big": "ok"})}
    good = {"diagnostics": _selftest({"ok": True, "ip": "132.0.0.9", "big": "ok"})}

    async def beat(body: dict) -> None:
        container.redis.store.pop(f"agent:hb:{device_id}", None)  # skip the rate limit
        assert (
            await http.post("/api/agent/heartbeat", headers=headers, json=body)
        ).status_code == 204

    await beat(leak)
    assert sent == []  # one bad report may be a restart's first seconds
    await beat(leak)
    assert len(sent) == 1 and sent[0].startswith("alerts:🔴") and "напрямую" in sent[0]
    await beat(leak)
    assert len(sent) == 1  # no repeats while it stays broken
    await beat(good)
    assert len(sent) == 2 and sent[1].startswith("alerts:🟢")

    row = (await http.get("/api/admin/routers", headers=await _login(http))).json()["items"][0]
    assert row["vpn"]["state"] == "ok"


async def test_hwid_stub_subscription_falls_back_to_panel_servers_quietly(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """Under a HWID device limit Remnawave answers our device-less request with an «App not
    supported» stub: no servers, routing intact. The router then gets panel-built servers (they
    work) plus the stub's split rules, and no false «params may be wrong» warning."""
    http, container = client
    container.remnawave_client.hosts = [_host("host-de", remark="DE", address="203.0.113.1")]
    container.remnawave_client.subscription_json = [
        {
            "remarks": "App not supported",
            "outbounds": [
                {"tag": "direct", "protocol": "freedom"},
                {"tag": "block", "protocol": "blackhole"},
            ],
            "routing": {
                "rules": [
                    {"type": "field", "domain": ["domain:ru"], "outboundTag": "direct"},
                    {"type": "field", "network": "tcp,udp", "balancerTag": "auto"},
                ],
                "balancers": [{"tag": "auto", "selector": ["proxy"], "fallbackTag": "direct"}],
            },
        }
    ]
    device_id, token = await _create_device(http, container, telegram_id=33, host_uuids=["host-de"])
    res = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    proxies = [o for o in res.json()["outbounds"] if o["tag"].startswith("proxy-")]
    assert [o["settings"]["vnext"][0]["address"] for o in proxies] == ["203.0.113.1"]
    # the stub's own rules are not trusted: the built-in split (local nets + RU TLDs) is used
    split = [r for r in res.json()["routing"]["rules"] if "inboundTag" not in r]
    assert split[1]["domain"] == ["domain:ru", "domain:su", "domain:xn--p1ai"]

    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=await _login(http))).json()
    assert detail["config_info"]["source"] == "panel"
    assert detail["config_info"]["warning"] is None
    assert detail["config_info"]["split_rules"] == 2


def test_clients_path_probe_outranks_the_server_probes() -> None:
    """Field: servers and balancer passed, the card said «VPN works», and no device on the
    network could open a page (fixed only by `xkeen -stop`). The routed probe follows the
    clients' path and decides."""
    from src.web.routes.agent import vpn_verdict

    good_balancer = {"ok": True, "ip": "132.0.0.9", "big": "ok"}

    def with_routed(routed: dict, agent_version: str = "4") -> dict:
        diag = _selftest(good_balancer)
        diag["agent_version"] = agent_version
        diag["self_test"].append({"name": "routed", **routed})
        return diag

    broken = vpn_verdict(with_routed({"ok": False, "ip": "", "error": "curl: (28) timeout"}))
    assert broken and broken["state"] == "fail" and "Устройства в сети" in broken["text"]
    leak = vpn_verdict(with_routed({"ok": True, "ip": "91.0.0.1"}))
    assert leak and leak["state"] == "direct" and "устройств" in leak["text"]
    fine = vpn_verdict(with_routed({"ok": True, "ip": "132.0.0.9"}))
    assert fine and fine["state"] == "ok"
    # a v3 agent mid self-update probes this path against our .ru server (direct by design)
    stale = vpn_verdict(with_routed({"ok": True, "ip": "91.0.0.1"}, agent_version="3"))
    assert stale and stale["state"] == "ok" and stale["routed"] is None


async def test_stub_subscription_never_steers_routing_or_dns(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-de", remark="DE", address="203.0.113.1")]
    container.remnawave_client.subscription_json = [
        {
            "remarks": "App not supported",
            "outbounds": [{"tag": "direct", "protocol": "freedom"}],
            "dns": {"servers": ["https://10.255.255.1/dns-query"]},
            "routing": {
                "domainStrategy": "IPOnDemand",
                "rules": [{"type": "field", "network": "tcp,udp", "outboundTag": "block"}],
            },
        }
    ]
    _device_id, token = await _create_device(
        http, container, telegram_id=34, host_uuids=["host-de"]
    )
    cfg = (await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})).json()
    assert "dns" not in cfg
    assert "domainStrategy" not in cfg["routing"]
    rules = cfg["routing"]["rules"]
    assert all(r.get("outboundTag") != "block" for r in rules)  # the stub's block-all is gone
    split = [r for r in rules if "inboundTag" not in r]
    assert {"domain": ["domain:ru", "domain:su", "domain:xn--p1ai"]}.items() <= split[1].items()
    assert split[-1]["balancerTag"] == "balancer"
    # the clients'-path probe listener has no rule of its own
    routed = [i for i in cfg["inbounds"] if i["tag"] == "cwtest-routed"]
    assert routed and routed[0]["port"] == 10868
    assert not [r for r in rules if r.get("inboundTag") == ["cwtest-routed"]]


async def test_auto_router_is_moved_to_what_works_from_its_isp(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """Owner's request: AUTO routers start on least-connections servers, then the router's own
    probes decide — fastest working server becomes the primary after the first check."""
    http, container = client
    container.remnawave_client.hosts = [
        _host("nl-host-1", remark="NL", address="203.0.113.1"),
        _host("fi-host-2", remark="FI", address="203.0.113.2"),
        _host("us-host-3", remark="US", address="203.0.113.3"),
    ]
    device_id, token = await _create_device(
        http, container, telegram_id=35, host_uuids=["nl-host-1", "fi-host-2", "us-host-3"]
    )
    auth = {"Authorization": f"Bearer {token}"}
    cfg = (await http.get("/api/agent/config", headers=auth)).json()
    proxies = sorted(o["tag"] for o in cfg["outbounds"] if o["tag"].startswith("proxy-"))
    cands = [o["tag"] for o in cfg["outbounds"] if o["tag"].startswith("cand-")]
    # Every allowlisted server is in the static probe set, the assigned ones included.
    assert len(proxies) == 2 and len(cands) == 3

    def probe(tag: str, ok: bool, secs: str = "") -> dict:
        return {
            "name": tag,
            "ok": ok,
            "ip": "x" if ok else "",
            "secs": secs,
            "big": "",
            "error": "",
        }

    by_tag = {
        "proxy-nl-host-": "nl",
        "proxy-fi-host-": "fi",
        "proxy-us-host-": "us",
        "cand-nl-host-": "nl",
        "cand-fi-host-": "fi",
        "cand-us-host-": "us",
    }
    speeds = {"nl": "0.90", "fi": "0.80", "us": "0.10"}  # the candidate is the fastest
    tests = [probe(t, True, speeds[by_tag[t]]) for t in [*proxies, *cands]]
    container.redis.store.pop(f"agent:hb:{device_id}", None)
    res = await http.post(
        "/api/agent/heartbeat",
        headers=auth,
        json={"diagnostics": {"agent_version": "5", "self_test": tests}},
    )
    assert res.status_code == 204

    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=await _login(http))).json()
    assert detail["primary_host_uuid"] == "us-host-3"
    assert "первая проверка" in detail["autotune"]["note"]
    assert "US (0.10 с)" in detail["autotune"]["note"]


async def test_past_end_date_means_vpn_off_and_no_alarm_before_the_status_flips(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch
) -> None:
    """Field: a trial ended, the panel already cut the UUID, but the stored status had not
    flipped yet — the router still got VPN config and the card went red «all servers down»."""
    import datetime as dt

    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    device_id, token = await _create_device(http, container, telegram_id=36, host_uuids=["host-a"])
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        sub = await uow.subscriptions.get(device.subscription_id)
        sub.expire_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)  # status still ACTIVE
        await uow.commit()

    auth = {"Authorization": f"Bearer {token}"}
    cfg = (await http.get("/api/agent/config", headers=auth)).json()
    assert [o["tag"] for o in cfg["outbounds"]] == ["direct", "block"]
    assert cfg["routing"]["rules"] == [
        {"type": "field", "network": "tcp,udp", "outboundTag": "direct"}
    ]

    sent: list[str] = []

    async def fake_report(_container, code: str, text: str, **_kw) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr("src.infrastructure.services.reports.send_topic_report", fake_report)
    dead = {"ok": False, "ip": "", "error": "curl: (35) unexpected eof"}
    for _ in range(3):
        container.redis.store.pop(f"agent:hb:{device_id}", None)
        await http.post(
            "/api/agent/heartbeat",
            headers=auth,
            json={"diagnostics": _selftest(dead)},
        )
    assert sent == []  # unpaid is not an outage
    row = (await http.get("/api/admin/routers", headers=await _login(http))).json()["items"][0]
    assert row["vpn"]["state"] == "expired"


# --- agent v7: event journal + game pulse -------------------------------------------------


async def test_pulse_settings_need_a_token_and_are_off_by_default(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    assert (await http.get("/api/agent/pulse")).status_code == 401
    bad = await http.get("/api/agent/pulse", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401

    container.remnawave_client.hosts = [_host("host-a")]
    device_id, token = await _create_device(http, container, telegram_id=40, host_uuids=["host-a"])
    auth = {"Authorization": f"Bearer {token}"}
    res = await http.get("/api/agent/pulse", headers=auth)
    assert res.status_code == 200
    assert res.json() == {"enabled": False, "every_runs": 2, "timeout_s": 3, "targets": []}
    assert (await http.get("/api/agent/pulse", headers=auth)).status_code == 429
    rev_off = (await http.get("/api/agent/config", headers=auth)).headers["X-Pulse-Rev"]

    # Switched on in the card: targets arrive and /config advertises a new revision, so the
    # agent re-reads /pulse on its next run instead of within the hour.
    admin = await _login(http)
    put = await http.put(
        f"/api/admin/routers/{device_id}/pulse", headers=admin, json={"enabled": True}
    )
    assert put.status_code == 200
    container.redis.store.pop(f"agent:pulsecfg:{device_id}", None)
    on = (await http.get("/api/agent/pulse", headers=auth)).json()
    assert on["enabled"] is True
    assert [t["id"] for t in on["targets"]] == ["aws-fra", "aws-sto", "rbx-fra"]
    assert on["targets"][2]["resolve_ip"] == "128.116.44.3"
    container.redis.store.pop(f"agent:cfg:{device_id}", None)
    rev_on = (await http.get("/api/agent/config", headers=auth)).headers["X-Pulse-Rev"]
    assert rev_on != rev_off

    # Other routers stay off.
    _, other = await _create_device(http, container, telegram_id=41, host_uuids=["host-a"])
    res = await http.get("/api/agent/pulse", headers={"Authorization": f"Bearer {other}"})
    assert res.json()["enabled"] is False


async def test_heartbeat_v7_stores_events_and_pulse_for_the_card(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    import time

    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=42)
    auth = {"Authorization": f"Bearer {token}"}
    now = int(time.time())
    events = [
        {"ts": now - 3600, "kind": "watchdog_stop", "note": "no server and no internet"},
        {"ts": now - 3300, "kind": "watchdog_resume", "note": ""},
        {"ts": now - 3290, "kind": "config_applied", "note": "etag=abc"},
    ]
    pulse = {
        "ts": now,
        "results": [
            {"target": "aws-fra", "path": "direct", "ms": 46},
            {"target": "aws-fra", "path": "proxy-de", "ms": 63},
            {"target": "aws-sto", "path": "direct", "ms": None},
        ],
    }
    body = {
        "xray_running": True,
        "diagnostics": {"agent_version": "7"},
        "events": events,
        "pulse": pulse,
    }
    assert (await http.post("/api/agent/heartbeat", headers=auth, json=body)).status_code == 204
    # Pulse not switched on for this router: its numbers are not kept.
    assert f"agent:pulse:{device_id}" not in container.redis.store

    admin = await _login(http)
    await http.put(f"/api/admin/routers/{device_id}/pulse", headers=admin, json={"enabled": True})
    # The agent re-sends its last 20 events every run: no duplicates.
    container.redis.store.pop(f"agent:hb:{device_id}", None)
    body["events"] = [*events, {"ts": now - 10, "kind": "config_applied", "note": ""}]
    assert (await http.post("/api/agent/heartbeat", headers=auth, json=body)).status_code == 204
    container.redis.store.pop(f"agent:hb:{device_id}", None)
    assert (await http.post("/api/agent/heartbeat", headers=auth, json=body)).status_code == 204

    stored = json.loads(container.redis.store[f"agent:events:{device_id}"])
    assert len(stored) == 4
    runs = json.loads(container.redis.store[f"agent:pulse:{device_id}"])
    assert len(runs) == 2 and runs[0]["r"][0] == ["aws-fra", "direct", 46]

    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=admin)).json()
    assert detail["restarts"]["day"] == 3  # watchdog stop + 2 config applies
    assert detail["restarts"]["supported"] is True
    assert detail["restarts"]["events"][0]["kind"] == "config_applied"  # newest first
    games = detail["games"]
    assert games["supported"] is True and games["enabled"] is True
    assert [p["id"] for p in games["paths"]] == ["direct", "proxy-de"]
    assert games["rows"][0]["best"] == "direct"
    assert games["rows"][1]["cells"]["direct"]["loss_24h"] == 1.0
    assert "быстрее напрямую" in games["verdict"]
    row = (await http.get("/api/admin/routers", headers=admin)).json()["items"][0]
    assert row["restarts_24h"] == 3


async def test_heartbeat_v6_without_events_or_pulse_still_works(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=43)
    res = await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"xray_running": True, "diagnostics": {"agent_version": "6", "self_test": []}},
    )
    assert res.status_code == 204
    assert f"agent:events:{device_id}" not in container.redis.store
    assert f"agent:pulse:{device_id}" not in container.redis.store

    admin = await _login(http)
    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=admin)).json()
    assert detail["diagnostics"]["agent_version"] == "6"
    assert detail["games"]["supported"] is False
    assert detail["games"]["rows"] and detail["games"]["paths"] == []
    assert detail["restarts"] == {"day": 0, "week": 0, "supported": False, "events": []}
    assert detail["restarts_24h"] == 0


async def test_heartbeat_ignores_malformed_events_and_pulse(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=44)
    res = await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": "nope", "pulse": [1, 2, 3], "diagnostics": {"agent_version": "7"}},
    )
    assert res.status_code == 204
    assert f"agent:events:{device_id}" not in container.redis.store
    assert f"agent:pulse:{device_id}" not in container.redis.store


async def test_heartbeat_with_junk_pulse_keeps_only_known_targets_and_survives_infinity(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    import random

    http, container = client
    device_id, token = await _create_device(http, container, telegram_id=45)
    admin = await _login(http)
    await http.put(f"/api/admin/routers/{device_id}/pulse", headers=admin, json={"enabled": True})
    rnd = random.Random(7)
    results = [
        {
            "target": rnd.choice(["aws-fra", "aws-sto", "evil", "x" * 40]),
            "path": rnd.choice(["direct", f"proxy-{i:08x}", "BAD PATH", "a" * 50]),
            "ms": rnd.randrange(500),
        }
        for i in range(40)
    ]
    # Raw JSON: Infinity / huge numbers must not turn into a 500.
    raw = json.dumps({"pulse": {"results": results}})[:-3] + (
        ', {"target": "aws-fra", "path": "direct", "ms": Infinity}]}, '
        '"events": [{"ts": Infinity, "kind": "rollback"}, {"ts": 1e400, "kind": "rollback"}]}'
    )
    res = await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        content=raw,
    )
    assert res.status_code == 204
    runs = json.loads(container.redis.store[f"agent:pulse:{device_id}"])
    rows = [row for run in runs for row in run["r"]]
    assert rows and len(rows) <= 20
    assert {row[0] for row in rows} <= {"aws-fra", "aws-sto"}
    assert len({row[1] for row in rows}) <= 6
    assert f"agent:events:{device_id}" not in container.redis.store
    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=admin)).json()
    assert len(detail["games"]["paths"]) <= 6
    assert [r["target"] for r in detail["games"]["rows"]] == ["aws-fra", "aws-sto", "rbx-fra"]


# --- «Мой роутер»: «Напрямую», the location pin, the v8 light check ------------------------


async def _set_device(container: ApiTestContainer, device_id: int, **fields: object) -> None:
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        for name, value in fields.items():
            setattr(device, name, value)
        await uow.commit()


async def _fetch(http: httpx.AsyncClient, container: ApiTestContainer, device_id: int, token: str):
    container.redis.store.pop(f"agent:cfg:{device_id}", None)
    return await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})


async def test_direct_mode_config_sends_everything_direct_and_keeps_probing(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    import datetime as dt

    http, container = client
    container.remnawave_client.hosts = [
        _host("host-aa", remark="NL", address="203.0.113.1"),
        _host("host-bb", remark="DE", address="203.0.113.2"),
        _host("host-cc", remark="FI", address="203.0.113.3"),
    ]
    device_id, token = await _create_device(
        http, container, telegram_id=50, host_uuids=["host-aa", "host-bb", "host-cc"]
    )
    vpn_cfg = (await _fetch(http, container, device_id, token)).json()

    until = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    await _set_device(container, device_id, direct_until=until)
    res = await _fetch(http, container, device_id, token)
    cfg = res.json()
    rules = cfg["routing"]["rules"]
    client_rules = [r for r in rules if "inboundTag" not in r]
    assert client_rules == [{"type": "field", "network": "tcp,udp", "outboundTag": "direct"}]
    # Everything the agent measures with is still there: outbounds, ports, observatory.
    assert [o["tag"] for o in cfg["outbounds"]] == [o["tag"] for o in vpn_cfg["outbounds"]]
    assert cfg["inbounds"] == vpn_cfg["inbounds"]
    assert cfg["burstObservatory"] == vpn_cfg["burstObservatory"]
    assert any(i["tag"] == "cwtest-cand-host-aa" for i in cfg["inbounds"])

    # The timer runs out: no sweep, just a different answer (etag and revision both move).
    etag_on = res.headers["ETag"]
    rev_on = res.headers["X-Config-Rev"]
    await _set_device(
        container, device_id, direct_until=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
    )
    off = await _fetch(http, container, device_id, token)
    assert off.headers["ETag"] != etag_on and off.headers["X-Config-Rev"] != rev_on
    assert off.json()["routing"]["rules"] == vpn_cfg["routing"]["rules"]


async def test_direct_mode_skips_vpn_alerts_and_autotune(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-aa", remark="NL", address="203.0.113.1")]
    device_id, token = await _create_device(http, container, telegram_id=51, host_uuids=["host-aa"])
    await _fetch(http, container, device_id, token)
    await _set_device(container, device_id, direct_forever=True)
    sent: list[str] = []

    async def fake_report(_container, code: str, text: str, **_kw) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr("src.infrastructure.services.reports.send_topic_report", fake_report)
    headers = {"Authorization": f"Bearer {token}"}
    leak = {"diagnostics": _selftest({"ok": True, "ip": "91.0.0.1", "big": "ok"})}
    for _ in range(3):
        container.redis.store.pop(f"agent:hb:{device_id}", None)
        assert (
            await http.post("/api/agent/heartbeat", headers=headers, json=leak)
        ).status_code == 204
    assert sent == []  # the customer's own «Напрямую» is not a broken VPN
    assert f"agent:tune:{device_id}" not in container.redis.store  # autotune left alone
    assert f"agent:vpn:{device_id}" not in container.redis.store

    row = (await http.get("/api/admin/routers", headers=await _login(http))).json()["items"][0]
    assert row["vpn"]["state"] == "paused" and row["direct_active"] is True


async def test_pin_survives_autotune_and_goes_when_host_leaves_the_allowlist(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [
        _host("nl-host-1", remark="NL", address="203.0.113.1"),
        _host("fi-host-2", remark="FI", address="203.0.113.2"),
        _host("us-host-3", remark="US", address="203.0.113.3"),
    ]
    device_id, token = await _create_device(
        http, container, telegram_id=52, host_uuids=["nl-host-1", "fi-host-2", "us-host-3"]
    )
    await _set_device(container, device_id, pinned_host_uuid="fi-host-2")
    res = await _fetch(http, container, device_id, token)
    proxies = [o["tag"] for o in res.json()["outbounds"] if o["tag"].startswith("proxy-")]
    assert proxies[0] == "proxy-fi-host-" and len(set(proxies)) == 2  # pin first, distinct backup

    def probe(tag: str, secs: str) -> dict:
        return {"name": tag, "ok": True, "ip": "x", "secs": secs, "big": "", "error": ""}

    all_tags = [o["tag"] for o in res.json()["outbounds"] if o["tag"][:5] in ("proxy", "cand-")]
    tests = [probe(t, "0.10" if "us" in t else "0.90") for t in all_tags]
    container.redis.store.pop(f"agent:hb:{device_id}", None)
    await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"diagnostics": {"agent_version": "8", "self_test": tests}},
    )
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
    assert device.pinned_host_uuid == "fi-host-2"
    assert device.primary_host_uuid == "us-host-3"  # autotune's own choice moved...
    res = await _fetch(http, container, device_id, token)
    proxies = [o["tag"] for o in res.json()["outbounds"] if o["tag"].startswith("proxy-")]
    assert proxies[0] == "proxy-fi-host-"  # ...but the pin still leads, and the backup differs
    assert proxies[1] != proxies[0]

    await http.put(
        "/api/admin/routers/config/eligible-hosts",
        headers=await _login(http),
        json={"host_uuids": ["nl-host-1", "us-host-3"]},
    )
    res = await _fetch(http, container, device_id, token)
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
    assert device.pinned_host_uuid is None  # back to «Авто», no action from anyone
    proxies = [o["tag"] for o in res.json()["outbounds"] if o["tag"].startswith("proxy-")]
    assert proxies[0] != "proxy-fi-host-"


async def test_failed_pin_notifies_owner_once_a_day_and_keeps_the_pin(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client

    class _Recording:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def notify_user(self, telegram_id: int, text: str) -> bool:
            self.sent.append((telegram_id, text))
            return True

    notifier = _Recording()
    container.notifier = notifier  # type: ignore[assignment]
    container.remnawave_client.hosts = [
        _host("nl-host-1", remark="NL", address="203.0.113.1"),
        _host("fi-host-2", remark="FI", address="203.0.113.2"),
    ]
    device_id, token = await _create_device(
        http, container, telegram_id=53, host_uuids=["nl-host-1", "fi-host-2"]
    )
    await _set_device(container, device_id, pinned_host_uuid="fi-host-2")
    await _fetch(http, container, device_id, token)

    tests = [{"name": "proxy-fi-host-", "ok": False, "ip": "", "secs": "", "big": "", "error": "x"}]

    async def report() -> None:
        container.redis.store.pop(f"agent:hb:{device_id}", None)
        await http.post(
            "/api/agent/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={"diagnostics": {"agent_version": "8", "self_test": tests}},
        )

    await report()
    assert notifier.sent == []  # one bad report may be a blip
    await report()
    await report()
    assert len(notifier.sent) == 1  # throttled: once a day, not every 5 minutes
    assert notifier.sent[0][0] == 53 and "резервный" in notifier.sent[0][1]
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
    assert device.pinned_host_uuid == "fi-host-2"

    # Location pings keep the time of the measurement for the cabinet.
    pings = json.loads(container.redis.store[f"agent:locpings:{device_id}"])
    assert pings["fi-host-2"]["ok"] is False and pings["fi-host-2"]["at"]


async def test_static_candidates_keep_the_etag_when_the_primary_changes(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [
        _host("host-aa", remark="NL", address="203.0.113.1"),
        _host("host-bb", remark="DE", address="203.0.113.2"),
        _host("host-cc", remark="FI", address="203.0.113.3"),
    ]
    device_id, token = await _create_device(
        http, container, telegram_id=54, host_uuids=["host-aa", "host-bb", "host-cc"]
    )
    first = await _fetch(http, container, device_id, token)
    second = await _fetch(http, container, device_id, token)
    assert first.headers["ETag"] == second.headers["ETag"]  # cycle to cycle: no restart
    cand = [o["tag"] for o in first.json()["outbounds"] if o["tag"].startswith("cand-")]
    assert cand == ["cand-host-aa", "cand-host-bb", "cand-host-cc"]

    # A different primary (autotune) reshuffles the proxies only — the probe set stays put.
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        current = (device.primary_host_uuid, device.backup_host_uuid)
    await _set_device(
        container, device_id, primary_host_uuid=current[1], backup_host_uuid=current[0]
    )
    moved = await _fetch(http, container, device_id, token)
    assert [o["tag"] for o in moved.json()["outbounds"] if o["tag"].startswith("cand-")] == cand


async def test_rev_endpoint_answers_without_panel_or_db_writes(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-aa", remark="NL", address="203.0.113.1")]
    device_id, token = await _create_device(http, container, telegram_id=55, host_uuids=["host-aa"])
    assert (await http.get("/api/agent/rev")).status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    cfg = await _fetch(http, container, device_id, token)
    rev = cfg.headers["X-Config-Rev"]

    async def no_panel(*_a: object, **_k: object) -> None:
        raise AssertionError("/rev must not call the panel")

    monkeypatch.setattr(container.remnawave_client, "get_hosts", no_panel)
    monkeypatch.setattr(container.remnawave_client, "get_user", no_panel)

    same = await http.get("/api/agent/rev", headers={**headers, "If-None-Match": f'"{rev}"'})
    assert same.status_code == 304 and same.content == b""
    assert (await http.get("/api/agent/rev", headers=headers)).status_code == 429

    # Switching «Напрямую» in the cabinet shows up on the next check.
    await _set_device(container, device_id, direct_forever=True)
    container.redis.store.pop(f"agent:rev:{device_id}", None)
    moved = await http.get("/api/agent/rev", headers={**headers, "If-None-Match": f'"{rev}"'})
    assert moved.status_code == 200 and moved.text != rev


async def test_older_agent_without_light_check_still_gets_direct_on_its_next_poll(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-aa", remark="NL", address="203.0.113.1")]
    device_id, token = await _create_device(http, container, telegram_id=56, host_uuids=["host-aa"])
    await _fetch(http, container, device_id, token)
    await _set_device(container, device_id, direct_forever=True)
    # A v7 agent: the same If-None-Match poll as ever, no /rev.
    cfg = await http.get(
        "/api/agent/config",
        headers={
            "Authorization": f"Bearer {token}",
            "If-None-Match": '"stale"',
        },
    )
    assert cfg.status_code == 429  # its 5-minute cadence never hits the 50 s window...
    container.redis.store.pop(f"agent:cfg:{device_id}", None)
    cfg = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    rules = [r for r in cfg.json()["routing"]["rules"] if "inboundTag" not in r]
    assert rules == [{"type": "field", "network": "tcp,udp", "outboundTag": "direct"}]


async def test_new_agent_goes_only_to_the_rollout_list(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    agent = await http.get("/api/agent/agent.sh")
    current = agent.text.split('AGENT_VERSION="', 1)[1].split('"', 1)[0]
    container.remnawave_client.hosts = [_host("host-a")]
    first_id, first = await _create_device(http, container, telegram_id=60, host_uuids=["host-a"])
    second_id, second = await _create_device(http, container, telegram_id=61, host_uuids=["host-a"])
    admin = await _login(http)

    async def advertised(device_id: int, token: str) -> str:
        res = await _fetch(http, container, device_id, token)
        return res.headers["X-Agent-Version"]

    # Nothing listed: nobody is offered the new agent.
    assert await advertised(first_id, first) == "7"
    put = await http.put(
        "/api/admin/routers/config/agent-rollout",
        headers=admin,
        json={"device_ids": [first_id]},
    )
    assert put.status_code == 200
    got = (await http.get("/api/admin/routers/config/agent-rollout", headers=admin)).json()
    assert got == {"all": False, "device_ids": [first_id]}
    assert await advertised(first_id, first) == current
    assert await advertised(second_id, second) == "7"

    # A router that already runs it is never told to go back.
    await http.put(
        "/api/admin/routers/config/agent-rollout", headers=admin, json={"device_ids": []}
    )
    container.redis.store.pop(f"agent:hb:{first_id}", None)
    await http.post(
        "/api/agent/heartbeat",
        headers={"Authorization": f"Bearer {first}"},
        json={"diagnostics": {"agent_version": current}},
    )
    assert await advertised(first_id, first) == current

    # «*» = every router.
    await http.put("/api/admin/routers/config/agent-rollout", headers=admin, json={"all": True})
    assert await advertised(second_id, second) == current


async def test_config_fetch_window_lets_the_light_check_through(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """The light check asks /config right after /rev answers «changed» — possibly half a minute
    after the full cycle's own fetch. The /config window must be shorter than that minute."""
    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    device_id, token = await _create_device(http, container, telegram_id=62, host_uuids=["host-a"])
    windows: dict[str, int | None] = {}
    original = container.redis.set

    async def recording_set(key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        windows[key] = ex
        return await original(key, value, nx=nx, ex=ex)

    container.redis.set = recording_set  # type: ignore[method-assign]
    await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    assert windows[f"agent:cfg:{device_id}"] == 30
