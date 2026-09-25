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
from dataclasses import dataclass
from typing import Any

from src.application.dto.panel import PanelHost

_VISION_FLOW = "xtls-rprx-vision"
# Reality+vision only applies to the tcp transport — Xray >=25.x renamed it "raw"; both
# spellings have been observed live on the same panel (see RemnawaveClient.get_hosts()).
_TCP_LIKE_NETWORKS = {"tcp", "raw"}

_DIRECT: dict[str, Any] = {"tag": "direct", "protocol": "freedom"}
_BLOCK: dict[str, Any] = {"tag": "block", "protocol": "blackhole"}
_BALANCER_TAG = "balancer"
_NON_PROXY_PROTOCOLS = {"freedom", "blackhole", "dns"}


@dataclass(frozen=True, slots=True)
class RoutingTemplate:
    """Split-tunnel rules lifted from the Xray-JSON subscription Happ receives, already
    retargeted at this router config's own tags (``direct``/``block``/``balancer``)."""

    rules: tuple[dict[str, Any], ...]
    fallback_tag: str | None = None
    domain_strategy: str | None = None
    domain_matcher: str | None = None
    dns: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": list(self.rules),
            "fallback_tag": self.fallback_tag,
            "domain_strategy": self.domain_strategy,
            "domain_matcher": self.domain_matcher,
            "dns": self.dns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoutingTemplate:
        return cls(
            rules=tuple(data.get("rules") or ()),
            fallback_tag=data.get("fallback_tag"),
            domain_strategy=data.get("domain_strategy"),
            domain_matcher=data.get("domain_matcher"),
            dns=data.get("dns"),
        )


def routing_template_from_subscription(payload: Any) -> RoutingTemplate | None:
    """``payload`` is a Remnawave Xray-JSON subscription (a list of full client configs, or a
    single one). Rules sending traffic to a proxy outbound or balancer are retargeted at the
    router's balancer; rules to freedom/blackhole outbounds keep going direct/block. Rules
    keyed on ``inboundTag`` are dropped (client-app inbounds don't exist on a router), as are
    rules to outbounds this config has no counterpart for (e.g. a ``dns-out``)."""
    configs = payload if isinstance(payload, list) else [payload]
    config = next((c for c in configs if isinstance(c, dict) and c.get("routing")), None)
    if config is None or not isinstance(config["routing"], dict):
        return None
    routing: dict[str, Any] = config["routing"]

    tag_map: dict[str, str] = {"direct": "direct", "block": "block"}
    proxy_tags: set[str] = set()
    for ob in config.get("outbounds") or []:
        if not isinstance(ob, dict) or not ob.get("tag"):
            continue
        protocol = ob.get("protocol")
        if protocol == "freedom":
            tag_map[ob["tag"]] = "direct"
        elif protocol == "blackhole":
            tag_map[ob["tag"]] = "block"
        elif protocol not in _NON_PROXY_PROTOCOLS:
            proxy_tags.add(ob["tag"])

    balancers = [b for b in routing.get("balancers") or [] if isinstance(b, dict)]
    fallback = next((b.get("fallbackTag") for b in balancers if b.get("fallbackTag")), None)

    rules: list[dict[str, Any]] = []
    for rule in routing.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("inboundTag"):
            continue
        out_tag = rule.get("outboundTag")
        base = {k: v for k, v in rule.items() if k not in ("outboundTag", "balancerTag")}
        if rule.get("balancerTag") or out_tag in proxy_tags or str(out_tag).startswith("proxy"):
            rules.append({**base, "balancerTag": _BALANCER_TAG})
        elif out_tag in tag_map:
            rules.append({**base, "outboundTag": tag_map[out_tag]})

    dns = config.get("dns") if isinstance(config.get("dns"), dict) else None
    return RoutingTemplate(
        rules=tuple(rules),
        fallback_tag=tag_map.get(fallback) if fallback else None,
        domain_strategy=routing.get("domainStrategy"),
        domain_matcher=routing.get("domainMatcher"),
        dns=dns,
    )


def build_outbounds(
    hosts: Sequence[PanelHost],
    *,
    vless_uuid: str,
    subscription_active: bool,
    template: RoutingTemplate | None = None,
) -> dict[str, Any]:
    """``hosts`` must already be the small candidate set picked for ONE router (its primary +
    optional backup) — never a whole squad.

    An unpaid subscription and "no eligible candidate hosts at all" (e.g. the admin's
    eligible-hosts allowlist is empty) both collapse to the same output: freedom-only, no proxy
    outbounds — so the customer keeps a working (if unprotected) connection instead of losing
    internet outright. Telling those two cases apart for alerting is the caller's job.

    ``template`` carries the admin's split-tunnel rules (RU services direct); without one,
    everything goes through the balancer.
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
        balancer: dict[str, Any] = {
            "tag": _BALANCER_TAG,
            "selector": ["proxy-"],
            "strategy": {"type": "leastPing"},
        }
        routing: dict[str, Any] = {"balancers": [balancer]}
        rules: list[dict[str, Any]] = []
        if template is not None:
            rules.extend(template.rules)
            if template.fallback_tag:
                balancer["fallbackTag"] = template.fallback_tag
            if template.domain_strategy:
                routing["domainStrategy"] = template.domain_strategy
            if template.domain_matcher:
                routing["domainMatcher"] = template.domain_matcher
            if template.dns:
                config["dns"] = template.dns
        rules.append({"type": "field", "network": "tcp,udp", "balancerTag": _BALANCER_TAG})
        routing["rules"] = rules
        config["routing"] = routing
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
