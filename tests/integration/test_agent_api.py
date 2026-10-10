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

    def with_routed(routed: dict) -> dict:
        diag = _selftest(good_balancer)
        diag["self_test"].append({"name": "routed", **routed})
        return diag

    broken = vpn_verdict(with_routed({"ok": False, "ip": "", "error": "curl: (28) timeout"}))
    assert broken and broken["state"] == "fail" and "Устройства в сети" in broken["text"]
    leak = vpn_verdict(with_routed({"ok": True, "ip": "91.0.0.1"}))
    assert leak and leak["state"] == "direct" and "устройств" in leak["text"]
    fine = vpn_verdict(with_routed({"ok": True, "ip": "132.0.0.9"}))
    assert fine and fine["state"] == "ok"


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
