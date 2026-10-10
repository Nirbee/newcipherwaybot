"""Router config generator — pure function, no HTTP/DB, easy to test in isolation.

Builds the Xray outbounds/observatory/routing a router's agent applies, from an already-
resolved small set of candidate hosts. Deciding WHICH hosts a given router gets to choose
between (RouterDevice.primary_host_uuid / backup_host_uuid — a least-connections sweep in AUTO
mode, an admin pin in FORCE mode) is the caller's job, not this module's: this only turns an
already-decided candidate set into a working config. Keeping that decision out of here is what
keeps this a pure, trivially-testable function.
"""

from __future__ import annotations

import copy
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
_NON_PROXY_PROTOCOLS = {"freedom", "blackhole", "dns", "loopback"}
# Loopback SOCKS inbounds for the agent's self-test: one per proxy outbound (is THIS server
# reachable and passing traffic?) plus one through the balancer (where does real traffic exit?).
# Routed first, so no split-tunnel rule can send a probe direct.
TEST_BALANCER_PORT = 10869
TEST_PORT_BASE = 10870
# A probe that gets NO routing rule of its own, so it travels exactly the path a LAN device's
# traffic does (split rules, domain strategy, DNS). The per-server/balancer probes skip all of
# that — a router once reported «VPN works» while its clients couldn't open a single page.
TEST_ROUTED_PORT = 10868
_TEST_TAG_PREFIX = "cwtest-"

# Split rules used when the subscription only returns Remnawave's «App not supported» stub
# (HWID limit on, our request has no device id): the stub's routing/DNS are not the customer's
# real template and must not steer a router. Local networks and Russian TLDs go direct,
# everything else through the VPN; no DNS override, no domain strategy (fewest moving parts).
SAFE_SPLIT_RULES: tuple[dict[str, Any], ...] = (
    {
        "type": "field",
        "ip": [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "100.64.0.0/10",
            "fc00::/7",
            "fe80::/10",
        ],
        "outboundTag": "direct",
    },
    {
        "type": "field",
        "domain": ["domain:ru", "domain:su", "domain:xn--p1ai"],
        "outboundTag": "direct",
    },
)


def is_stub_subscription(payload: Any) -> bool:
    """Remnawave's placeholder answer («App not supported» / HWID) instead of a real
    subscription."""
    configs = payload if isinstance(payload, list) else [payload]
    return any(
        isinstance(c, dict) and "not supported" in str(c.get("remarks") or "").lower()
        for c in configs
    )


def safe_split_template() -> RoutingTemplate:
    return RoutingTemplate(rules=SAFE_SPLIT_RULES, fallback_tag="direct")


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
    subscription_outbounds: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``hosts`` must already be the small candidate set picked for ONE router (its primary +
    optional backup) — never a whole squad.

    An unpaid subscription and "no eligible candidate hosts at all" (e.g. the admin's
    eligible-hosts allowlist is empty) both collapse to the same output: freedom-only, no proxy
    outbounds — so the customer keeps a working (if unprotected) connection instead of losing
    internet outright. Telling those two cases apart for alerting is the caller's job.

    ``template`` carries the admin's split-tunnel rules (RU services direct); without one,
    everything goes through the balancer.

    ``subscription_outbounds`` (see :func:`select_subscription_outbounds`) are the proxy
    outbounds exactly as the customer's Happ receives them — preferred over rebuilding them
    from panel host data, which can drift from what the node actually accepts.
    """
    proxies: list[dict[str, Any]] = []
    if subscription_active:
        if subscription_outbounds is not None:
            proxies = [copy.deepcopy(o) for o in subscription_outbounds]
        else:
            for host in hosts:
                if host.is_disabled or host.protocol != "vless":
                    continue
                outbound = _proxy_outbound(host, vless_uuid)
                if outbound is not None:
                    proxies.append(outbound)
    proxy_tags = [str(o["tag"]) for o in proxies if str(o.get("tag", "")).startswith("proxy-")]

    config: dict[str, Any] = {"outbounds": [*proxies, _DIRECT, _BLOCK]}
    if proxy_tags:
        # Server picked the (1-2) candidates; the router's own observatory/balancer only
        # needs to pick the better of THOSE by live ping — cheap even with just one candidate.
        # burstObservatory, not the plain one: the plain observatory marks a server dead on ONE
        # failed probe (5 s timeout) and keeps it dead until the next probe 5 minutes later —
        # a single hiccup on both servers sent every connection direct (VPN off) for minutes,
        # while both servers passed the agent's own self-test (field, router «TEST»). Here a
        # server is dead only when ALL of its last 3 pings failed (Xray: Alive = All != Fail),
        # each pinged ~once a minute with a 10 s timeout.
        config["burstObservatory"] = {
            "subjectSelector": ["proxy-"],
            "pingConfig": {
                "destination": "https://www.gstatic.com/generate_204",
                "interval": "1m",
                "sampling": 3,
                "timeout": "10s",
            },
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
        inbounds, test_rules = _self_test_plumbing(proxy_tags)
        config["inbounds"] = inbounds
        routing["rules"] = test_rules + rules
        config["routing"] = routing
    return config


def _self_test_plumbing(
    proxy_tags: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def socks(tag: str, port: int) -> dict[str, Any]:
        return {
            "tag": tag,
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        }

    inbounds = [
        socks(f"{_TEST_TAG_PREFIX}routed", TEST_ROUTED_PORT),  # no rule: the clients' path
        socks(f"{_TEST_TAG_PREFIX}balancer", TEST_BALANCER_PORT),
    ]
    rules: list[dict[str, Any]] = [
        {
            "type": "field",
            "inboundTag": [f"{_TEST_TAG_PREFIX}balancer"],
            "balancerTag": _BALANCER_TAG,
        }
    ]
    for i, tag in enumerate(proxy_tags):
        inbounds.append(socks(f"{_TEST_TAG_PREFIX}{tag}", TEST_PORT_BASE + i))
        rules.append(
            {"type": "field", "inboundTag": [f"{_TEST_TAG_PREFIX}{tag}"], "outboundTag": tag}
        )
    return inbounds, rules


# --- proxies straight from the customer's Xray-JSON subscription --------------------------


@dataclass(frozen=True, slots=True)
class SubscriptionProxy:
    """One server as the customer's Happ receives it: the proxy outbound plus any outbounds
    its ``sockopt.dialerProxy`` chain needs (e.g. a fragment dialer)."""

    remark: str
    address: str
    port: int
    outbound: dict[str, Any]
    chain: tuple[dict[str, Any], ...] = ()


def _endpoint(outbound: dict[str, Any]) -> tuple[str, int] | None:
    settings = outbound.get("settings") or {}
    for key in ("vnext", "servers"):
        items = settings.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            first = items[0]
            if first.get("address"):
                return str(first["address"]), int(first.get("port") or 0)
    if settings.get("address"):  # Xray 25+ flat vless/vmess settings
        return str(settings["address"]), int(settings.get("port") or 0)
    return None


def _dialer(outbound: dict[str, Any]) -> str | None:
    sockopt = (outbound.get("streamSettings") or {}).get("sockopt") or {}
    value = sockopt.get("dialerProxy")
    return str(value) if value else None


def proxies_from_subscription(payload: Any) -> list[SubscriptionProxy]:
    """Every server in a Remnawave Xray-JSON subscription (a list of per-server client configs,
    or one config). Only servers the customer's squads actually grant appear there — which is
    exactly the set a router can use with this customer's UUID."""
    configs = payload if isinstance(payload, list) else [payload]
    out: list[SubscriptionProxy] = []
    seen: set[tuple[str, int]] = set()
    for config in configs:
        if not isinstance(config, dict):
            continue
        outbounds = [o for o in config.get("outbounds") or [] if isinstance(o, dict)]
        by_tag = {str(o["tag"]): o for o in outbounds if o.get("tag")}
        dialers = {d for d in (_dialer(o) for o in outbounds) if d}
        for ob in outbounds:
            if ob.get("protocol") in _NON_PROXY_PROTOCOLS or str(ob.get("tag")) in dialers:
                continue
            endpoint = _endpoint(ob)
            if endpoint is None or endpoint in seen:
                continue
            chain: list[dict[str, Any]] = []
            nxt = _dialer(ob)
            while nxt and nxt in by_tag and len(chain) < 3:
                chain.append(by_tag[nxt])
                nxt = _dialer(by_tag[nxt])
            seen.add(endpoint)
            out.append(
                SubscriptionProxy(
                    remark=str(config.get("remarks") or ob.get("tag") or ""),
                    address=endpoint[0],
                    port=endpoint[1],
                    outbound=ob,
                    chain=tuple(chain),
                )
            )
    return out


@dataclass(frozen=True, slots=True)
class SubscriptionPick:
    outbounds: list[dict[str, Any]]
    servers: list[str]  # human names, in priority order
    warning: str | None
    tags: dict[str, str]  # outbound tag -> human name (labels the agent's per-server probes)


def _tagged(sp: SubscriptionProxy, tag: str) -> list[dict[str, Any]]:
    """The proxy outbound under ``tag`` plus its renamed dialer chain (never ``proxy-*``, so
    the observatory/balancer selector doesn't mistake a fragment dialer for a server)."""
    ob = copy.deepcopy(sp.outbound)
    ob["tag"] = tag
    extra: list[dict[str, Any]] = []
    prev = ob
    for i, link in enumerate(sp.chain):
        renamed = copy.deepcopy(link)
        renamed["tag"] = f"cwdial-{tag.removeprefix('proxy-')}-{i}"
        sockopt = prev.setdefault("streamSettings", {}).setdefault("sockopt", {})
        sockopt["dialerProxy"] = renamed["tag"]
        extra.append(renamed)
        prev = renamed
    # A dialer this config doesn't carry would make xray refuse the whole file.
    last_sockopt = (prev.get("streamSettings") or {}).get("sockopt") or {}
    if last_sockopt.get("dialerProxy") and not str(last_sockopt["dialerProxy"]).startswith(
        "cwdial-"
    ):
        last_sockopt.pop("dialerProxy", None)
    return [ob, *extra]


def _fallback_tag(address: str, port: int) -> str:
    return "proxy-" + hashlib.sha256(f"{address}:{port}".encode()).hexdigest()[:8]


def select_subscription_outbounds(
    available: Sequence[SubscriptionProxy],
    assigned: Sequence[PanelHost],
    *,
    preferred: Sequence[PanelHost] = (),
    limit: int = 2,
) -> SubscriptionPick:
    """Map the router's assigned servers (primary, backup) onto the customer's subscription.

    A server the customer's subscription doesn't include can't work — the node rejects this
    UUID there, the balancer's probes fail and every connection falls back to ``direct`` (the
    field failure: a router online, VPN "on", traffic leaving with the ISP's address). Such a
    server is replaced by one the subscription does have, preferring the admin's router
    allowlist (``preferred``), and the substitution is reported instead of staying silent."""
    by_endpoint = {(p.address, p.port): p for p in available}
    chosen: list[tuple[str, SubscriptionProxy]] = []
    missing: list[str] = []
    for host in assigned:
        sp = by_endpoint.get((host.address, host.port))
        if sp is None:
            missing.append(host.remark or host.address)
        elif all(sp is not c for _, c in chosen):
            chosen.append((f"proxy-{host.uuid[:8]}", sp))

    want = min(limit, len(assigned)) if assigned else limit
    if len(chosen) < want:
        pref = {(h.address, h.port): h for h in preferred}
        ordered = [p for p in available if (p.address, p.port) in pref] + [
            p for p in available if (p.address, p.port) not in pref
        ]
        for sp in ordered:
            if len(chosen) >= want:
                break
            if any(sp is c for _, c in chosen):
                continue
            known = pref.get((sp.address, sp.port))
            tag = (
                f"proxy-{known.uuid[:8]}"
                if known is not None
                else _fallback_tag(sp.address, sp.port)
            )
            chosen.append((tag, sp))

    outbounds: list[dict[str, Any]] = []
    for tag, sp in chosen:
        outbounds.extend(_tagged(sp, tag))
    warning = None
    if missing:
        used = ", ".join(sp.remark for _, sp in chosen) or "нет"
        warning = (
            f"Сервер(ы) {', '.join(missing)} не входят в подписку клиента (сквады тарифа) — "
            f"через них роутер не работал бы. Использую серверы из подписки: {used}."
        )
    elif not assigned and chosen:
        warning = "Серверы роутеру не назначены — использую серверы из подписки клиента."
    return SubscriptionPick(
        outbounds,
        [sp.remark for _, sp in chosen],
        warning,
        {tag: sp.remark for tag, sp in chosen},
    )


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


def hosts_within_squads(
    assigned: Sequence[PanelHost],
    user_squads: Sequence[str],
    candidates: Sequence[PanelHost] = (),
) -> tuple[list[PanelHost], str | None]:
    """Keep only assigned servers the customer's internal squads grant (a node rejects the UUID
    on any other inbound), topping up from ``candidates`` (the admin's router allowlist) that
    the squads do grant. Unknown squad data on either side = no filtering (can't judge)."""
    granted = set(user_squads)

    def allowed(h: PanelHost) -> bool:
        return not granted or not h.squad_uuids or bool(granted & set(h.squad_uuids))

    kept = [h for h in assigned if allowed(h)]
    missing = [h.remark or h.address for h in assigned if not allowed(h)]
    if not missing:
        return kept, None
    for h in candidates:
        if len(kept) >= len(assigned):
            break
        if h.is_disabled or h.protocol != "vless" or not allowed(h):
            continue
        if all(h.uuid != k.uuid for k in kept):
            kept.append(h)
    used = ", ".join(h.remark for h in kept) or "нет подходящих"
    return kept, (
        f"Сервер(ы) {', '.join(missing)} не входят в сквады подписки клиента — нода не пустила бы "
        f"его ключ. Использую: {used}."
    )
