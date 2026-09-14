"""Router config generator — pure function, no HTTP/DB, easy to test in isolation.

Builds the Xray outbounds/observatory/routing a router's agent applies, from an already-
resolved small set of candidate hosts. Deciding WHICH hosts a given router gets to choose
between (RouterDevice.primary_host_uuid / backup_host_uuid — a least-connections sweep in AUTO
mode, an admin pin in FORCE mode) is the caller's job, not this module's: this only turns an
already-decided candidate set into a working config. Keeping that decision out of here is what
keeps this a pure, trivially-testable function.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from src.application.dto.panel import PanelHost

_VISION_FLOW = "xtls-rprx-vision"
# Reality+vision only applies to the tcp transport — Xray >=25.x renamed it "raw"; both
# spellings have been observed live on the same panel (see RemnawaveClient.get_hosts()).
_TCP_LIKE_NETWORKS = {"tcp", "raw"}

_DIRECT: dict[str, Any] = {"tag": "direct", "protocol": "freedom"}
_BLOCK: dict[str, Any] = {"tag": "block", "protocol": "blackhole"}


def build_outbounds(
    hosts: Sequence[PanelHost], *, vless_uuid: str, subscription_active: bool
) -> dict[str, Any]:
    """``hosts`` must already be the small candidate set picked for ONE router (its primary +
    optional backup) — never a whole squad.

    An unpaid subscription and "no eligible candidate hosts at all" (e.g. the admin's
    eligible-hosts allowlist is empty) both collapse to the same output: freedom-only, no proxy
    outbounds — so the customer keeps a working (if unprotected) connection instead of losing
    internet outright. Telling those two cases apart for alerting is the caller's job.
    """
    proxies: list[dict[str, Any]] = []
    if subscription_active:
        for host in hosts:
            if host.is_disabled or host.protocol != "vless":
                continue
            outbound = _proxy_outbound(host, vless_uuid)
            if outbound is not None:
                proxies.append(outbound)

    config: dict[str, Any] = {"outbounds": [*proxies, _DIRECT, _BLOCK]}
    if proxies:
        # Server picked the (1-2) candidates; the router's own observatory/balancer only
        # needs to pick the better of THOSE by live ping — cheap even with just one candidate.
        config["observatory"] = {
            "subjectSelector": ["proxy-"],
            "probeUrl": "https://www.gstatic.com/generate_204",
            "probeInterval": "5m",
        }
        config["routing"] = {
            "balancers": [
                {"tag": "balancer", "selector": ["proxy-"], "strategy": {"type": "leastPing"}}
            ],
            "rules": [{"type": "field", "network": "tcp,udp", "balancerTag": "balancer"}],
        }
    return config


def _proxy_outbound(host: PanelHost, vless_uuid: str) -> dict[str, Any] | None:
    if host.security == "reality" and not host.public_key:
        # Can't build a working Reality outbound without a public key — derivation failed
        # (bad/missing private key data), so skip rather than ship a broken outbound.
        return None

    user: dict[str, Any] = {"id": vless_uuid, "encryption": "none"}
    if host.network in _TCP_LIKE_NETWORKS and host.security == "reality":
        user["flow"] = _VISION_FLOW

    stream: dict[str, Any] = {"network": host.network, "security": host.security}
    if host.security == "reality":
        stream["realitySettings"] = {
            "show": False,
            "fingerprint": host.fingerprint or "chrome",
            "serverName": host.sni or "",
            "publicKey": host.public_key,
            # "" is a valid, panel-verified value (any/no short id required) — never replaced.
            "shortId": host.short_id,
        }
    elif host.security == "tls":
        stream["tlsSettings"] = {
            "serverName": host.sni or "",
            "fingerprint": host.fingerprint or "chrome",
        }

    if host.network == "xhttp":
        xhttp: dict[str, Any] = {"path": host.path or "/", "mode": host.xhttp_mode or "auto"}
        if host.sni:
            xhttp["host"] = host.sni
        stream["xhttpSettings"] = xhttp
    elif host.network == "grpc":
        stream["grpcSettings"] = {"serviceName": host.service_name or "", "multiMode": False}
    elif host.network == "ws":
        ws: dict[str, Any] = {"path": host.path or "/"}
        if host.sni:
            ws["headers"] = {"Host": host.sni}
        stream["wsSettings"] = ws

    return {
        "tag": f"proxy-{host.uuid[:8]}",
        "protocol": "vless",
        "settings": {"vnext": [{"address": host.address, "port": host.port, "users": [user]}]},
        "streamSettings": stream,
    }


def config_etag(config: dict[str, Any]) -> str:
    """Stable hash of a generated config regardless of dict insertion order, so the agent's
    If-None-Match can skip re-sending an unchanged config."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
