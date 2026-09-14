"""get_hosts(): host<->inbound<->squad resolution, verified against a live 3.0 panel's real
response shape (see docs in client.py's get_hosts docstring) — hosts and inbounds are separate
Remnawave resources cross-referenced by uuid, and squad MEMBERSHIP is defined from the squad
side (each internal squad embeds its member inbounds), not the host side (which only carries
an EXCLUDE-list). None of the key material here is a real panel secret — this is a fresh
X25519 keypair generated solely for this test.
"""

from __future__ import annotations

import httpx
import respx

from src.infrastructure.remnawave.client import (
    RemnawaveHttpClient,
    _derive_reality_public_key,
    _to_panel_host,
)
from src.core.config.remnawave import PanelAuthType, RemnawaveSettings
from src.infrastructure.remnawave.connection import build_profile

BASE = "https://panel.example.com"

# Freshly generated for this test only — not a production key.
_TEST_PRIVATE_KEY = "GLaknx_HEenkaJuMZeXned3olulPOKwi6LtU2zs8V1k"
_TEST_PUBLIC_KEY = "c-i5ggNipCtGIyqBVjcbctf3jLltOcsuLwS1q5RQanI"


def _client() -> RemnawaveHttpClient:
    cfg = RemnawaveSettings(base_url=BASE, auth_type=PanelAuthType.API_KEY, token="secret")
    return RemnawaveHttpClient.from_profile(build_profile(cfg))


def _mock_v3() -> None:
    respx.get(f"{BASE}/api/system/metadata").mock(
        return_value=httpx.Response(200, json={"response": {"version": "3.0.0"}})
    )


def _mock_v2() -> None:
    respx.get(f"{BASE}/api/system/metadata").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/api/system/health").mock(
        return_value=httpx.Response(200, json={"response": {"version": "2.8.0"}})
    )


def _reality_inbound(
    *, uuid: str, tag: str, network: str, short_ids: list[str] | None = None,
    extra_stream: dict[str, object] | None = None,
) -> dict[str, object]:
    """Shape verified live: internal-squads[i].inbounds[] embeds the full inbound, same as
    /api/config-profiles' flattened `inbounds` — a host resolves against this by uuid."""
    stream: dict[str, object] = {
        "network": network,
        "security": "reality",
        "realitySettings": {
            "dest": "example.com:443",
            "show": False,
            "xver": 0,
            "shortIds": short_ids if short_ids is not None else ["abcd1234"],
            "privateKey": _TEST_PRIVATE_KEY,
            "serverNames": ["example.com"],
        },
    }
    if extra_stream:
        stream.update(extra_stream)
    return {
        "uuid": uuid,
        "profileUuid": "profile-1",
        "tag": tag,
        "type": "vless",
        "network": network,
        "security": "reality",
        "port": 443,
        "rawInbound": {
            "tag": tag,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": stream,
        },
    }


def _hysteria_inbound(*, uuid: str) -> dict[str, object]:
    return {
        "uuid": uuid,
        "profileUuid": "profile-1",
        "tag": "Hysteria2",
        "type": "hysteria",
        "network": "hysteria",
        "security": "tls",
        "port": 443,
        "rawInbound": {
            "tag": "Hysteria2",
            "protocol": "hysteria",
            "settings": {"clients": [], "version": 2},
            "streamSettings": {"network": "hysteria", "security": "tls"},
        },
    }


def _host(*, uuid: str, inbound_uuid: str, excluded_squads: list[str] | None = None,
          **overrides: object) -> dict[str, object]:
    base = {
        "uuid": uuid,
        "remark": "Test host",
        "address": "203.0.113.10",
        "port": 8443,
        "sni": "example.com",
        "fingerprint": "chrome",
        "path": None,
        "isDisabled": False,
        "inbound": {"configProfileUuid": "profile-1", "configProfileInboundUuid": inbound_uuid},
        "excludedInternalSquads": excluded_squads or [],
    }
    base.update(overrides)
    return base


def test_derive_reality_public_key_matches_x25519() -> None:
    assert _derive_reality_public_key(_TEST_PRIVATE_KEY) == _TEST_PUBLIC_KEY


def test_derive_reality_public_key_handles_garbage() -> None:
    assert _derive_reality_public_key(None) is None
    assert _derive_reality_public_key("") is None
    assert _derive_reality_public_key("not-a-valid-key!!") is None


def test_to_panel_host_maps_tcp_reality() -> None:
    inbound = _reality_inbound(uuid="ib-1", tag="MP-REALITY-TCP", network="tcp")
    host = _host(uuid="host-1", inbound_uuid="ib-1")
    result = _to_panel_host(host, inbound, ("squad-a",))
    assert result.protocol == "vless"
    assert result.network == "tcp"
    assert result.security == "reality"
    assert result.public_key == _TEST_PUBLIC_KEY
    assert result.short_id == "abcd1234"
    assert result.sni == "example.com"
    assert result.fingerprint == "chrome"
    assert result.address == "203.0.113.10"
    assert result.port == 8443
    assert result.squad_uuids == ("squad-a",)
    assert result.path is None
    assert result.service_name is None


def test_to_panel_host_maps_xhttp_path_and_mode() -> None:
    inbound = _reality_inbound(
        uuid="ib-2", tag="VLESS-XHTTP", network="xhttp",
        extra_stream={"xhttpSettings": {"mode": "auto", "path": "/abc123"}},
    )
    host = _host(uuid="host-2", inbound_uuid="ib-2")
    result = _to_panel_host(host, inbound, ())
    assert result.network == "xhttp"
    assert result.path == "/abc123"
    assert result.xhttp_mode == "auto"


def test_to_panel_host_maps_grpc_service_name() -> None:
    inbound = _reality_inbound(
        uuid="ib-3", tag="VLESS-GRPC", network="grpc",
        extra_stream={"grpcSettings": {"multiMode": False, "serviceName": "vless-grpc"}},
    )
    host = _host(uuid="host-3", inbound_uuid="ib-3")
    result = _to_panel_host(host, inbound, ())
    assert result.network == "grpc"
    assert result.service_name == "vless-grpc"


def test_to_panel_host_empty_short_id_is_valid() -> None:
    """The panel legitimately ships `shortIds: [""]` (any/no short id required) — must not be
    dropped or replaced with a placeholder, just carried through as an empty string."""
    inbound = _reality_inbound(uuid="ib-4", tag="ZAPRET", network="raw", short_ids=[""])
    host = _host(uuid="host-4", inbound_uuid="ib-4")
    result = _to_panel_host(host, inbound, ())
    assert result.short_id == ""


def test_to_panel_host_does_not_filter_hysteria() -> None:
    """get_hosts()/its mapper is a faithful mapping, not a router-specific filter — callers
    (the config generator) are responsible for excluding Hysteria2 hosts."""
    inbound = _hysteria_inbound(uuid="ib-5")
    host = _host(uuid="host-5", inbound_uuid="ib-5")
    result = _to_panel_host(host, inbound, ())
    assert result.protocol == "hysteria"
    assert result.public_key is None  # no realitySettings on this inbound at all


@respx.mock
async def test_get_hosts_resolves_squad_membership() -> None:
    _mock_v3()
    inbound = _reality_inbound(uuid="ib-1", tag="MP-REALITY-TCP", network="tcp")
    respx.get(f"{BASE}/api/internal-squads").mock(
        return_value=httpx.Response(
            200,
            json={"response": {"internalSquads": [{"uuid": "squad-a", "inbounds": [inbound]}]}},
        )
    )
    respx.get(f"{BASE}/api/hosts").mock(
        return_value=httpx.Response(
            200, json={"response": {"hosts": [_host(uuid="host-1", inbound_uuid="ib-1")]}}
        )
    )
    client = _client()
    try:
        hosts = await client.get_hosts()
    finally:
        await client.aclose()
    assert len(hosts) == 1
    assert hosts[0].uuid == "host-1"
    assert hosts[0].squad_uuids == ("squad-a",)


@respx.mock
async def test_get_hosts_respects_excluded_squads() -> None:
    _mock_v3()
    inbound = _reality_inbound(uuid="ib-1", tag="MP-REALITY-TCP", network="tcp")
    respx.get(f"{BASE}/api/internal-squads").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "internalSquads": [
                        {"uuid": "squad-a", "inbounds": [inbound]},
                        {"uuid": "squad-b", "inbounds": [inbound]},
                    ]
                }
            },
        )
    )
    respx.get(f"{BASE}/api/hosts").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "hosts": [_host(uuid="host-1", inbound_uuid="ib-1", excluded_squads=["squad-b"])]
                }
            },
        )
    )
    client = _client()
    try:
        hosts = await client.get_hosts()
    finally:
        await client.aclose()
    assert hosts[0].squad_uuids == ("squad-a",)


@respx.mock
async def test_get_hosts_skips_host_with_unknown_inbound() -> None:
    """A host referencing an inbound this client couldn't find (e.g. deleted config profile)
    must be dropped, not raise — one bad host shouldn't break the whole router config."""
    _mock_v3()
    respx.get(f"{BASE}/api/internal-squads").mock(
        return_value=httpx.Response(200, json={"response": {"internalSquads": []}})
    )
    respx.get(f"{BASE}/api/hosts").mock(
        return_value=httpx.Response(
            200, json={"response": {"hosts": [_host(uuid="host-1", inbound_uuid="missing")]}}
        )
    )
    client = _client()
    try:
        hosts = await client.get_hosts()
    finally:
        await client.aclose()
    assert hosts == []


@respx.mock
async def test_get_hosts_returns_empty_on_v2_panel() -> None:
    """Reality/transport params live on a resource shape only ever observed on 3.0 — rather
    than guess at an unverified 2.x format, get_hosts() intentionally no-ops there."""
    _mock_v2()
    squads_route = respx.get(f"{BASE}/api/internal-squads").mock(
        return_value=httpx.Response(200, json={"response": {"internalSquads": []}})
    )
    client = _client()
    try:
        hosts = await client.get_hosts()
    finally:
        await client.aclose()
    assert hosts == []
    assert not squads_route.called
