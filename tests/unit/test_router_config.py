"""build_outbounds() / config_etag(): pure config-generation rules, no HTTP/DB."""

from __future__ import annotations

import json

from src.application.dto.panel import PanelHost
from src.application.services.router_config import (
    RoutingTemplate,
    build_outbounds,
    config_etag,
    proxies_from_subscription,
    routing_template_from_subscription,
    select_subscription_outbounds,
)

VLESS_UUID = "11111111-2222-3333-4444-555555555555"


def _host(**overrides: object) -> PanelHost:
    base: dict[str, object] = {
        "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "remark": "Test host",
        "address": "203.0.113.10",
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


def test_tcp_reality_gets_vision_flow() -> None:
    cfg = build_outbounds([_host(network="tcp")], vless_uuid=VLESS_UUID, subscription_active=True)
    proxy = cfg["outbounds"][0]
    assert proxy["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"


def test_raw_reality_also_gets_vision_flow() -> None:
    """Xray >=25.x renamed the tcp transport to "raw" — both spellings seen live."""
    cfg = build_outbounds([_host(network="raw")], vless_uuid=VLESS_UUID, subscription_active=True)
    proxy = cfg["outbounds"][0]
    assert proxy["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"


def test_xhttp_reality_gets_no_flow_but_gets_path_and_mode() -> None:
    cfg = build_outbounds(
        [_host(network="xhttp", path="/abc123", xhttp_mode="auto")],
        vless_uuid=VLESS_UUID,
        subscription_active=True,
    )
    proxy = cfg["outbounds"][0]
    assert "flow" not in proxy["settings"]["vnext"][0]["users"][0]
    assert proxy["streamSettings"]["xhttpSettings"] == {
        "path": "/abc123",
        "mode": "auto",
        "host": "example.com",
    }


def test_grpc_reality_gets_no_flow_but_gets_service_name() -> None:
    cfg = build_outbounds(
        [_host(network="grpc", service_name="vless-grpc")],
        vless_uuid=VLESS_UUID,
        subscription_active=True,
    )
    proxy = cfg["outbounds"][0]
    assert "flow" not in proxy["settings"]["vnext"][0]["users"][0]
    assert proxy["streamSettings"]["grpcSettings"]["serviceName"] == "vless-grpc"


def test_inactive_subscription_gets_freedom_only_no_uuid_leaked() -> None:
    cfg = build_outbounds([_host()], vless_uuid=VLESS_UUID, subscription_active=False)
    tags = [o["tag"] for o in cfg["outbounds"]]
    assert tags == ["direct", "block"]
    assert "observatory" not in cfg
    assert "routing" not in cfg
    assert VLESS_UUID not in json.dumps(cfg)


def test_no_eligible_hosts_also_collapses_to_freedom_only() -> None:
    cfg = build_outbounds([], vless_uuid=VLESS_UUID, subscription_active=True)
    tags = [o["tag"] for o in cfg["outbounds"]]
    assert tags == ["direct", "block"]
    assert "observatory" not in cfg


def test_hysteria_hosts_are_filtered_out() -> None:
    cfg = build_outbounds(
        [_host(protocol="hysteria", network="hysteria", security="tls")],
        vless_uuid=VLESS_UUID,
        subscription_active=True,
    )
    tags = [o["tag"] for o in cfg["outbounds"]]
    assert tags == ["direct", "block"]


def test_disabled_hosts_are_filtered_out() -> None:
    cfg = build_outbounds(
        [_host(is_disabled=True)], vless_uuid=VLESS_UUID, subscription_active=True
    )
    tags = [o["tag"] for o in cfg["outbounds"]]
    assert tags == ["direct", "block"]


def test_reality_host_missing_public_key_is_skipped() -> None:
    cfg = build_outbounds([_host(public_key=None)], vless_uuid=VLESS_UUID, subscription_active=True)
    tags = [o["tag"] for o in cfg["outbounds"]]
    assert tags == ["direct", "block"]


def test_empty_short_id_is_preserved_not_replaced() -> None:
    cfg = build_outbounds([_host(short_id="")], vless_uuid=VLESS_UUID, subscription_active=True)
    proxy = cfg["outbounds"][0]
    assert proxy["streamSettings"]["realitySettings"]["shortId"] == ""


def test_primary_and_backup_both_become_balancer_candidates() -> None:
    primary = _host(uuid="11111111-1111-1111-1111-111111111111", address="203.0.113.1")
    backup = _host(uuid="22222222-2222-2222-2222-222222222222", address="203.0.113.2")
    cfg = build_outbounds([primary, backup], vless_uuid=VLESS_UUID, subscription_active=True)
    proxy_tags = [o["tag"] for o in cfg["outbounds"] if o["tag"].startswith("proxy-")]
    assert len(proxy_tags) == 2
    assert cfg["routing"]["balancers"][0]["selector"] == ["proxy-"]


def test_config_etag_is_stable_regardless_of_key_order() -> None:
    a = {"outbounds": [{"tag": "direct", "protocol": "freedom"}], "extra": 1}
    b = {"extra": 1, "outbounds": [{"protocol": "freedom", "tag": "direct"}]}
    assert config_etag(a) == config_etag(b)


def test_config_etag_changes_when_content_changes() -> None:
    a = build_outbounds([_host()], vless_uuid=VLESS_UUID, subscription_active=True)
    b = build_outbounds([_host()], vless_uuid=VLESS_UUID, subscription_active=False)
    assert config_etag(a) != config_etag(b)


# --- split-tunnel template (rules lifted from the Xray-JSON subscription Happ receives) ---

HAPP_SUBSCRIPTION = [
    {
        "remarks": "x",
        "outbounds": [
            {"tag": "proxy", "protocol": "vless"},
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
            {"tag": "dns-out", "protocol": "dns"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "domainMatcher": "hybrid",
            "rules": [
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "direct"},
                {"type": "field", "ip": ["10.0.0.0/8"], "outboundTag": "direct"},
                {"type": "field", "domain": ["domain:gosuslugi.ru"], "outboundTag": "direct"},
                {"type": "field", "inboundTag": ["socks"], "outboundTag": "proxy"},
                {"type": "field", "port": "53", "outboundTag": "dns-out"},
                {"type": "field", "domain": ["domain:x.com"], "outboundTag": "proxy"},
                {"type": "field", "network": "tcp,udp", "balancerTag": "auto"},
            ],
            "balancers": [
                {"tag": "auto", "selector": ["proxy"], "fallbackTag": "direct"},
            ],
        },
        "dns": {"servers": ["1.1.1.1"], "queryStrategy": "UseIP"},
    }
]


def test_template_keeps_direct_rules_and_retargets_proxy_rules_at_balancer() -> None:
    tpl = routing_template_from_subscription(HAPP_SUBSCRIPTION)
    assert tpl is not None
    targets = [(r.get("outboundTag"), r.get("balancerTag")) for r in tpl.rules]
    assert targets == [
        ("direct", None),
        ("direct", None),
        ("direct", None),
        (None, "balancer"),
        (None, "balancer"),
    ]
    assert tpl.rules[2]["domain"] == ["domain:gosuslugi.ru"]
    assert tpl.fallback_tag == "direct"
    assert tpl.domain_strategy == "IPIfNonMatch"
    assert tpl.dns == {"servers": ["1.1.1.1"], "queryStrategy": "UseIP"}


def test_template_drops_inbound_and_unknown_outbound_rules() -> None:
    tpl = routing_template_from_subscription(HAPP_SUBSCRIPTION)
    assert tpl is not None
    assert not any("inboundTag" in r for r in tpl.rules)
    assert not any(r.get("port") == "53" for r in tpl.rules)


def test_template_absent_routing_returns_none() -> None:
    assert routing_template_from_subscription([{"outbounds": []}]) is None
    assert routing_template_from_subscription("not json") is None


def test_build_with_template_puts_split_rules_before_catch_all() -> None:
    tpl = routing_template_from_subscription(HAPP_SUBSCRIPTION)
    cfg = build_outbounds([_host()], vless_uuid=VLESS_UUID, subscription_active=True, template=tpl)
    # Self-test probes are routed first (by inbound tag); the split rules follow them.
    rules = [r for r in cfg["routing"]["rules"] if "inboundTag" not in r]
    assert rules[0]["outboundTag"] == "direct"
    assert rules[-1] == {"type": "field", "network": "tcp,udp", "balancerTag": "balancer"}
    assert cfg["routing"]["balancers"][0]["fallbackTag"] == "direct"
    assert cfg["routing"]["domainStrategy"] == "IPIfNonMatch"
    assert cfg["dns"]["servers"] == ["1.1.1.1"]
    tags = {o["tag"] for o in cfg["outbounds"]}
    assert {"direct", "block"} <= tags
    for rule in rules:
        assert rule.get("outboundTag") in (None, "direct", "block")
        assert rule.get("balancerTag") in (None, "balancer")


def test_inactive_subscription_ignores_template() -> None:
    tpl = routing_template_from_subscription(HAPP_SUBSCRIPTION)
    cfg = build_outbounds([_host()], vless_uuid=VLESS_UUID, subscription_active=False, template=tpl)
    assert "routing" not in cfg
    assert "dns" not in cfg


def test_template_round_trips_through_dict() -> None:
    tpl = routing_template_from_subscription(HAPP_SUBSCRIPTION)
    assert tpl is not None
    assert RoutingTemplate.from_dict(json.loads(json.dumps(tpl.to_dict()))) == tpl


# --- servers straight from the customer's subscription (what their Happ actually uses) ------


def _sub_server(remark: str, address: str, port: int = 443, **stream: object) -> dict:
    return {
        "remarks": remark,
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": address,
                            "port": port,
                            "users": [{"id": "customer-uuid", "encryption": "none"}],
                        }
                    ]
                },
                "streamSettings": {"network": "xhttp", "security": "reality", **stream},
            },
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
    }


def test_subscription_servers_are_used_verbatim_under_router_tags() -> None:
    payload = [
        _sub_server("🇩🇪 DE", "203.0.113.1", xhttpSettings={"path": "/x", "extra": {"a": 1}}),
        _sub_server("🇳🇱 NL", "203.0.113.2"),
    ]
    subs = proxies_from_subscription(payload)
    assert [p.remark for p in subs] == ["🇩🇪 DE", "🇳🇱 NL"]
    pick = select_subscription_outbounds(
        subs,
        [
            _host(uuid="aaaaaaaa-1", address="203.0.113.1"),
            _host(uuid="bbbbbbbb-2", address="203.0.113.2"),
        ],
    )
    assert pick.warning is None
    assert [o["tag"] for o in pick.outbounds] == ["proxy-aaaaaaaa", "proxy-bbbbbbbb"]
    # the node-specific extras Happ gets survive untouched
    assert pick.outbounds[0]["streamSettings"]["xhttpSettings"]["extra"] == {"a": 1}
    user = pick.outbounds[0]["settings"]["vnext"][0]["users"][0]
    assert user["id"] == "customer-uuid"

    cfg = build_outbounds(
        [], vless_uuid="", subscription_active=True, subscription_outbounds=pick.outbounds
    )
    assert [o["tag"] for o in cfg["outbounds"]] == [
        "proxy-aaaaaaaa",
        "proxy-bbbbbbbb",
        "direct",
        "block",
    ]


def test_server_outside_the_customers_squads_is_replaced_and_reported() -> None:
    """The field failure: the assigned server wasn't in the customer's subscription, the node
    rejected the UUID, every probe failed and traffic silently left via ``direct``."""
    subs = proxies_from_subscription([_sub_server("🇳🇱 NL", "203.0.113.2")])
    pick = select_subscription_outbounds(
        subs, [_host(uuid="aaaaaaaa-1", remark="DE", address="203.0.113.9")]
    )
    assert pick.servers == ["🇳🇱 NL"]
    assert pick.warning and "DE" in pick.warning and "не входят в подписку" in pick.warning
    assert pick.outbounds[0]["settings"]["vnext"][0]["address"] == "203.0.113.2"


def test_unassigned_router_still_gets_the_customers_servers() -> None:
    subs = proxies_from_subscription(
        [
            _sub_server("A", "203.0.113.1"),
            _sub_server("B", "203.0.113.2"),
            _sub_server("C", "203.0.113.3"),
        ]
    )
    pick = select_subscription_outbounds(subs, [])
    assert pick.servers == ["A", "B"]  # at most two candidates
    assert pick.warning


def test_dialer_chain_is_carried_and_renamed_out_of_the_proxy_selector() -> None:
    server = _sub_server("DE", "203.0.113.1", sockopt={"dialerProxy": "fragment"})
    server["outbounds"].append(
        {"tag": "fragment", "protocol": "freedom", "settings": {"fragment": {"length": "1-3"}}}
    )
    subs = proxies_from_subscription([server])
    assert len(subs) == 1  # the fragment dialer is not a server of its own
    pick = select_subscription_outbounds(subs, [_host(uuid="aaaaaaaa-1", address="203.0.113.1")])
    tags = [o["tag"] for o in pick.outbounds]
    assert tags == ["proxy-aaaaaaaa", "cwdial-aaaaaaaa-0"]
    assert pick.outbounds[0]["streamSettings"]["sockopt"]["dialerProxy"] == "cwdial-aaaaaaaa-0"


def test_dangling_dialer_reference_is_dropped() -> None:
    subs = proxies_from_subscription(
        [_sub_server("DE", "203.0.113.1", sockopt={"dialerProxy": "missing", "mark": 255})]
    )
    pick = select_subscription_outbounds(subs, [_host(uuid="aaaaaaaa-1", address="203.0.113.1")])
    assert pick.outbounds[0]["streamSettings"]["sockopt"] == {"mark": 255}


def test_self_test_inbounds_route_each_probe_to_its_server() -> None:
    cfg = build_outbounds(
        [
            _host(uuid="aaaaaaaa-1", address="203.0.113.1"),
            _host(uuid="bbbbbbbb-2", address="203.0.113.2"),
        ],
        vless_uuid=VLESS_UUID,
        subscription_active=True,
    )
    ports = {i["tag"]: i["port"] for i in cfg["inbounds"]}
    assert ports == {
        "cwtest-balancer": 10869,
        "cwtest-proxy-aaaaaaaa": 10870,
        "cwtest-proxy-bbbbbbbb": 10871,
    }
    assert all(i["listen"] == "127.0.0.1" for i in cfg["inbounds"])
    rules = cfg["routing"]["rules"]
    assert rules[0] == {
        "type": "field",
        "inboundTag": ["cwtest-balancer"],
        "balancerTag": "balancer",
    }
    assert rules[1]["outboundTag"] == "proxy-aaaaaaaa"
    # no proxies (unpaid) -> no probe listeners either
    assert "inbounds" not in build_outbounds([_host()], vless_uuid="x", subscription_active=False)


def test_hosts_outside_the_customers_squads_are_swapped_for_granted_ones() -> None:
    from src.application.services.router_config import hosts_within_squads

    de = _host(uuid="de-host", remark="DE", squad_uuids=("sq-premium",))
    nl = _host(uuid="nl-host", remark="NL", squad_uuids=("sq-default",))
    kept, warning = hosts_within_squads([de], ("sq-default",), [de, nl])
    assert [h.remark for h in kept] == ["NL"]
    assert warning and "DE" in warning
    # granted, or squads unknown on either side -> untouched, no warning
    assert hosts_within_squads([nl], ("sq-default",)) == ([nl], None)
    assert hosts_within_squads([de], ()) == ([de], None)
