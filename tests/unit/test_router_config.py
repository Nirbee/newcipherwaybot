"""build_outbounds() / config_etag(): pure config-generation rules, no HTTP/DB."""

from __future__ import annotations

import json

from src.application.dto.panel import PanelHost
from src.application.services.router_config import build_outbounds, config_etag

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
        vless_uuid=VLESS_UUID, subscription_active=True,
    )
    proxy = cfg["outbounds"][0]
    assert "flow" not in proxy["settings"]["vnext"][0]["users"][0]
    assert proxy["streamSettings"]["xhttpSettings"] == {
        "path": "/abc123", "mode": "auto", "host": "example.com"
    }


def test_grpc_reality_gets_no_flow_but_gets_service_name() -> None:
    cfg = build_outbounds(
        [_host(network="grpc", service_name="vless-grpc")],
        vless_uuid=VLESS_UUID, subscription_active=True,
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
        vless_uuid=VLESS_UUID, subscription_active=True,
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
    cfg = build_outbounds(
        [_host(public_key=None)], vless_uuid=VLESS_UUID, subscription_active=True
    )
    tags = [o["tag"] for o in cfg["outbounds"]]
    assert tags == ["direct", "block"]


def test_empty_short_id_is_preserved_not_replaced() -> None:
    cfg = build_outbounds(
        [_host(short_id="")], vless_uuid=VLESS_UUID, subscription_active=True
    )
    proxy = cfg["outbounds"][0]
    assert proxy["streamSettings"]["realitySettings"]["shortId"] == ""


def test_primary_and_backup_both_become_balancer_candidates() -> None:
    primary = _host(uuid="11111111-1111-1111-1111-111111111111", address="203.0.113.1")
    backup = _host(uuid="22222222-2222-2222-2222-222222222222", address="203.0.113.2")
    cfg = build_outbounds(
        [primary, backup], vless_uuid=VLESS_UUID, subscription_active=True
    )
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
