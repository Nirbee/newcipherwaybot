"""Router agent API: what a customer's XKeen/Xray router itself polls.

Deliberately NOT under /api/admin (staff-only JWT) — this is a separate, narrower surface
authenticated by one bearer token per RouterDevice (never a query param, so it can't end up in
a reverse-proxy access log). The token is looked up by its sha256 hash; the plain value only
ever existed in the admin API's create/rotate responses.

Panel-unavailable is NOT the same as "no hosts assigned"/"unpaid": a genuine failure to reach
Remnawave must 503 rather than silently hand back a freedom-only config — the agent keeps
whatever config it already has on a 503, but a 200 with an empty proxy list is taken as the
deliberate truth and applied. Collapsing those two states would mean a panel blip disables VPN
for the whole router fleet at once.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from src.application.services.router_config import (
    RoutingTemplate,
    build_outbounds,
    config_etag,
    hosts_within_squads,
    proxies_from_subscription,
    routing_template_from_subscription,
    select_subscription_outbounds,
)
from src.core.enums import RouterDeviceStatus
from src.core.logging import get_logger
from src.infrastructure.database.models.router_device import RouterDevice
from src.infrastructure.di import AppContainer
from src.web.deps import get_container

log = get_logger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])
# Short install links (/i/<code>) live at the site root so the command stays short enough
# to type from a phone screen into the router's terminal.
short_router = APIRouter(tags=["agent"])

# A hair under the agent's 5-minute poll interval's floor — generous margin for a misbehaving
# script or clock drift, never tight enough to bite a legitimately-timed poll.
_RATE_LIMIT_SECONDS = 50
_LAST_ERROR_MAX = 512
_TEMPLATE_TTL_SECONDS = 600
_DIAGNOSTICS_MAX = 16_384

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "router-agent"


def _read_script(name: str) -> str:
    return (_SCRIPTS_DIR / name).read_text(encoding="utf-8")


def _agent_version() -> str:
    try:
        match = re.search(r'^AGENT_VERSION="([^"]+)"', _read_script("agent.sh"), re.M)
    except OSError:
        return ""
    return match.group(1) if match else ""


# Advertised on every /config reply; an agent seeing a different value fetches /agent.sh and
# replaces itself — so a fix ships to the whole fleet with a deploy, no SSH to any router.
_AGENT_VERSION = _agent_version()


# Xray the installer puts on a router: the version the Remnawave nodes run, not whatever is
# newest. A field router on a fresh release failed while an otherwise identical one on the
# nodes' version worked — client and server cores are kept in step deliberately.
ROUTER_XRAY_VERSION = "v26.7.28"


# --- short install codes ------------------------------------------------------------------
# The full install command carries a 43-char token — technicians read it off a phone and retype
# it into PowerShell, and typos broke installs. A short code (6 chars, no look-alike symbols)
# maps to the token in redis for a day: `curl -fsSL <base>/i/K7PX2Q | sh`.
INSTALL_CODE_TTL_SECONDS = 24 * 3600
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
_CODE_LEN = 6
_CODE_LOOKUPS_PER_WINDOW = 30
_CODE_LOOKUP_WINDOW_SECONDS = 600


async def issue_install_code(container: AppContainer, device_id: int, token: str) -> str | None:
    """New short code for this device's (fresh) token; the previous code stops working.
    None when redis is unavailable — the UI then falls back to the long command."""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    try:
        old = await _redis_get(container, f"agent:icdev:{device_id}")
        if old:
            await container.redis.delete(f"agent:ic:{old}")
        await container.redis.set(
            f"agent:ic:{code}", f"{device_id}:{token}", ex=INSTALL_CODE_TTL_SECONDS
        )
        await container.redis.set(f"agent:icdev:{device_id}", code, ex=INSTALL_CODE_TTL_SECONDS)
    except Exception:
        log.warning("agent: install code not stored", device_id=device_id)
        return None
    return code


async def current_install_code(container: AppContainer, device_id: int) -> str | None:
    code = await _redis_get(container, f"agent:icdev:{device_id}")
    if code and await _redis_get(container, f"agent:ic:{code}"):
        return code
    return None


async def drop_install_code(container: AppContainer, device_id: int) -> None:
    try:
        code = await _redis_get(container, f"agent:icdev:{device_id}")
        if code:
            await container.redis.delete(f"agent:ic:{code}")
        await container.redis.delete(f"agent:icdev:{device_id}")
    except Exception:
        log.warning("agent: install code not dropped", device_id=device_id)


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _install_error(text: str) -> PlainTextResponse:
    # Still a (tiny) shell script: the command pipes it into `sh`, so a plain 404 body would
    # only produce curl's English error — this prints the reason in Russian instead.
    return PlainTextResponse(f"#!/bin/sh\necho {_sh_quote('!!! ' + text)}\nexit 1\n")


@short_router.get("/i/{code}", response_class=PlainTextResponse)
async def short_install(
    code: str, request: Request, container: AppContainer = Depends(get_container)
) -> PlainTextResponse:
    """`curl -fsSL <base>/i/<code> | sh` — a wrapper that downloads the full installer to a file
    and runs it with the device token (stdin detached, so nothing in it can swallow the piped
    script)."""
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (
        request.client.host if request.client else "?"
    )
    try:
        key = f"agent:ic:rl:{ip}"
        hits = int(await container.redis.incr(key))
        if hits == 1:
            await container.redis.expire(key, _CODE_LOOKUP_WINDOW_SECONDS)
    except Exception:
        hits = 0
    if hits > _CODE_LOOKUPS_PER_WINDOW:
        return _install_error("Слишком много попыток. Подождите 10 минут и повторите.")
    stored = await _redis_get(container, f"agent:ic:{code.strip().upper()}")
    if not stored or ":" not in stored:
        return _install_error(
            "Код установки не найден или устарел (действует 24 часа). Откройте карточку роутера "
            "в админке и возьмите новую команду."
        )
    _device_id, token = stored.split(":", 1)
    async with container.uow() as uow:
        device = await uow.router_devices.by_token_hash(_hash_token(token))
    if device is None or device.status is RouterDeviceStatus.REVOKED:
        return _install_error("Этот роутер удалён или отозван в админке — создайте его заново.")
    base = (container.settings.web.public_url or "").strip().rstrip("/") or str(
        request.base_url
    ).rstrip("/")
    return PlainTextResponse(
        "#!/bin/sh\n"
        "# CipherWay: установка агента на роутер (короткая ссылка)\n"
        f"curl -fsSL {_sh_quote(base + '/api/agent/install.sh')} -o /tmp/cw-install.sh || "
        "{ echo '!!! Не удалось скачать установщик — проверьте интернет на роутере'; exit 1; }\n"
        f"sh /tmp/cw-install.sh {_sh_quote(token)} {_sh_quote(base)} </dev/null\n"
    )


# --- GitHub mirror for XKeen ---------------------------------------------------------------
# Some ISPs freeze connections to GitHub after ~16 KB, so XKeen and Xray never finish
# downloading on the router (seen in the field). The installer points XKeen's own «gh_proxy»
# setting here; the server fetches from GitHub instead. Only the repositories XKeen actually
# installs from are served — this is not an open proxy.
_GH_HOSTS = {"github.com", "raw.githubusercontent.com", "api.github.com", "codeload.github.com"}
_GH_OWNERS = {
    "jameszerox",
    "xtls",
    "metacubex",
    "mikefarah",
    "1andrevich",
    "v2fly",
    "loyalsoldier",
    "runetfreedom",
}
_GH_REQUESTS_PER_WINDOW = 200
_GH_WINDOW_SECONDS = 600


def github_mirror_target(target: str, query: str = "") -> str | None:
    """``https://github.com/<owner>/...`` (as XKeen appends it to the prefix) -> the upstream
    URL, or None when it isn't one of the allowed GitHub hosts/owners."""
    target = target.strip()
    for scheme in ("https://", "https:/"):  # a proxy may have merged the double slash
        if target.startswith(scheme):
            target = target[len(scheme) :]
            break
    else:
        return None
    host, _, path = target.partition("/")
    host = host.lower()
    if host not in _GH_HOSTS or ".." in path:
        return None
    parts = [x for x in path.split("/") if x]
    owner = parts[1] if host == "api.github.com" and parts[:1] == ["repos"] else None
    if host != "api.github.com":
        owner = parts[0] if parts else None
    if not owner or owner.lower() not in _GH_OWNERS:
        return None
    return f"https://{host}/{path}" + (f"?{query}" if query else "")


@short_router.api_route("/gh/{target:path}", methods=["GET", "HEAD"])
async def github_mirror(
    target: str, request: Request, container: AppContainer = Depends(get_container)
) -> Response:
    url = github_mirror_target(target, request.url.query)
    if url is None:
        raise HTTPException(403, "only XKeen's GitHub downloads are mirrored")
    try:
        key = f"agent:gh:rl:{_caller_ip(request)}"
        hits = int(await container.redis.incr(key))
        if hits == 1:
            await container.redis.expire(key, _GH_WINDOW_SECONDS)
    except Exception:
        hits = 0
    if hits > _GH_REQUESTS_PER_WINDOW:
        raise HTTPException(429, "too many requests")

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))
    upstream = client.build_request(
        request.method,
        url,
        headers={
            "User-Agent": "cipherway-xkeen-mirror",
            "Accept": request.headers.get("accept", "*/*"),
            # identity: pass bytes through untouched so Content-Length stays truthful
            "Accept-Encoding": "identity",
        },
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        log.warning("gh mirror: upstream failed", url=url, error=str(exc))
        raise HTTPException(502, "GitHub unreachable from the server") from exc
    headers = {
        k: resp.headers[k]
        for k in ("content-type", "content-length", "etag", "last-modified")
        if k in resp.headers
    }
    if request.method == "HEAD" or resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        if request.method != "HEAD":
            headers.pop("content-length", None)
        return Response(status_code=resp.status_code, headers=headers)

    async def body() -> Any:
        try:
            async for chunk in resp.aiter_raw(65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=headers)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _rate_limit_ok(container: AppContainer, key: str) -> bool:
    try:
        return bool(await container.redis.set(key, "1", nx=True, ex=_RATE_LIMIT_SECONDS))
    except Exception:
        return True  # a redis hiccup must not block a legitimate poll


async def _redis_get(container: AppContainer, key: str) -> str | None:
    try:
        value = await container.redis.get(key)
    except Exception:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


async def _redis_set(container: AppContainer, key: str, value: str, ex: int | None) -> None:
    try:
        await container.redis.set(key, value, ex=ex)
    except Exception:
        log.warning("agent: redis write failed", key=key)


async def _subscription_payload(
    container: AppContainer, subscription_id: int, subscription_url: str | None
) -> Any:
    """The customer's Xray-JSON subscription exactly as Happ receives it (servers + split-tunnel
    rules), cached per subscription. A failed refresh falls back to the last good copy so a
    subscription-page blip doesn't flip every router's config back and forth."""
    fresh_key = f"agent:sub:{subscription_id}"
    stale_key = f"agent:sub:{subscription_id}:last"
    cached = await _redis_get(container, fresh_key)
    if cached is not None:
        return json.loads(cached)
    if subscription_url:
        try:
            payload = await container.remnawave_client.fetch_subscription_json(subscription_url)
        except Exception as exc:
            log.warning("agent: subscription json fetch failed", error=str(exc))
        else:
            encoded = json.dumps(payload)
            await _redis_set(container, fresh_key, encoded, _TEMPLATE_TTL_SECONDS)
            if payload:
                await _redis_set(container, stale_key, encoded, None)
            return payload
    stale = await _redis_get(container, stale_key)
    return json.loads(stale) if stale else None


async def _eligible_hosts(container: AppContainer) -> set[str]:
    async with container.uow() as uow:
        row = await uow.bot_config.find_one(key="ROUTER_ELIGIBLE_HOSTS")
    return {str(v) for v in ((row.value if row is not None else None) or [])}


async def config_info(container: AppContainer, device_id: int) -> dict[str, Any] | None:
    """What the last /config actually gave this router: which servers, from where, and any
    substitution warning — shown on the router card."""
    raw = await _redis_get(container, f"agent:cfginfo:{device_id}")
    return json.loads(raw) if raw else None


async def require_device(
    authorization: str | None = Header(None),
    container: AppContainer = Depends(get_container),
) -> RouterDevice:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "missing bearer token")
    async with container.uow() as uow:
        device = await uow.router_devices.by_token_hash(_hash_token(token))
    if device is None:
        raise HTTPException(401, "unknown token")
    if device.status is RouterDeviceStatus.REVOKED:
        raise HTTPException(403, "device revoked")
    return device


@router.get("/agent.sh", response_class=PlainTextResponse)
async def agent_script() -> str:
    """Public: the script holds no secrets — the token lives only in the router's agent.conf."""
    return _read_script("agent.sh")


@router.get("/install.sh", response_class=PlainTextResponse)
async def install_script() -> str:
    return _read_script("install.sh")


@router.get("/whoami")
async def whoami(
    device: RouterDevice = Depends(require_device),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Token check for the installer — no rate limit, so it doesn't eat the agent's first
    /config poll; tells the technician which customer this router is being set up for."""
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(device.subscription_id)
        owner = await uow.users.get(sub.user_id) if sub else None
    client = None
    if owner is not None:
        client = (f"@{owner.username}" if owner.username else owner.first_name or owner.email) or (
            str(owner.telegram_id) if owner.telegram_id else None
        )
    return {
        "id": device.id,
        "label": device.label,
        "client": client,
        "xray_version": ROUTER_XRAY_VERSION,
    }


@router.get("/config")
async def get_config(
    response: Response,
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    device: RouterDevice = Depends(require_device),
    container: AppContainer = Depends(get_container),
) -> Any:
    if not await _rate_limit_ok(container, f"agent:cfg:{device.id}"):
        raise HTTPException(429, "too many requests")

    async with container.uow() as uow:
        sub = await uow.subscriptions.get(device.subscription_id)
        if sub is None:
            raise HTTPException(404, "subscription not found")
        owner = await uow.users.get(sub.user_id)

    panel_ref = sub.panel_ref
    vless_uuid = ""
    hosts: list[Any] = []
    all_hosts: list[Any] = []
    template: RoutingTemplate | None = None
    payload: Any = None
    user_squads: tuple[str, ...] = ()
    if panel_ref is not None:
        # Same telegram_id-attachment every other panel_ref caller needs (see client.py's
        # _v3_id): a uuid/short_id-only ref can't resolve on a v3 panel without it.
        panel_ref = replace(panel_ref, telegram_id=owner.telegram_id if owner else None)
        try:
            panel_user = await container.remnawave_client.get_user(panel_ref)
            all_hosts = await container.remnawave_client.get_hosts()
            by_uuid = {h.uuid: h for h in all_hosts}
            hosts = [
                by_uuid[u]
                for u in (device.primary_host_uuid, device.backup_host_uuid)
                if u and u in by_uuid
            ]
        except Exception as exc:
            log.warning("agent config: panel unavailable", device_id=device.id, error=str(exc))
            raise HTTPException(503, "panel temporarily unavailable") from exc
        vless_uuid = (panel_user.vless_uuid if panel_user else None) or ""
        user_squads = tuple(panel_user.internal_squads) if panel_user else ()
        payload = await _subscription_payload(
            container, sub.id, panel_user.subscription_url if panel_user else None
        )
        template = routing_template_from_subscription(payload) if payload else None

    # Servers come from the customer's own subscription (the exact outbounds their Happ uses,
    # and only servers their squads grant). Rebuilding from panel host data is the fallback for
    # when the subscription page can't be read at all.
    # With a HWID device limit on, Remnawave answers a request without a device id (ours — a
    # router must not take one of the customer's device slots) with an «App not supported»
    # stub: routing and DNS intact, but no servers. Then the servers are built from panel data
    # (proven on a live router) and checked against the customer's squads directly.
    sub_proxies = proxies_from_subscription(payload) if payload else []
    subscription_outbounds = None
    eligible = await _eligible_hosts(container)
    if sub_proxies:
        pick = select_subscription_outbounds(
            sub_proxies, hosts, preferred=[h for h in all_hosts if h.uuid in eligible]
        )
        subscription_outbounds = pick.outbounds
        info: dict[str, Any] = {
            "source": "subscription",
            "servers": pick.servers,
            "warning": pick.warning,
            "tags": pick.tags,
        }
    else:
        hosts, squad_warning = hosts_within_squads(
            hosts, user_squads, [h for h in all_hosts if h.uuid in eligible]
        )
        info = {
            "source": "panel",
            "servers": [h.remark for h in hosts],
            "tags": {f"proxy-{h.uuid[:8]}": h.remark for h in hosts},
            "warning": squad_warning,
        }
    info["split_rules"] = len(template.rules) if template else 0
    if not sub.status.is_usable:
        info = {"source": "none", "servers": [], "warning": "Подписка не активна — VPN выключен."}
    await _redis_set(
        container, f"agent:cfginfo:{device.id}", json.dumps(info, ensure_ascii=False), 86400
    )

    config = build_outbounds(
        hosts,
        vless_uuid=vless_uuid,
        subscription_active=sub.status.is_usable,
        template=template,
        subscription_outbounds=subscription_outbounds,
    )
    etag = config_etag(config)

    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=304, headers={"X-Agent-Version": _AGENT_VERSION})

    async with container.uow() as uow:
        fresh = await uow.router_devices.get(device.id)
        if fresh is not None:
            fresh.config_etag = etag
            await uow.commit()

    response.headers["ETag"] = f'"{etag}"'
    response.headers["X-Agent-Version"] = _AGENT_VERSION
    return config


# --- self-test support ------------------------------------------------------------------

# Random (incompressible) bytes: big enough to cross the ~16 KB point where some Russian ISPs
# freeze connections to foreign hosts, which small probes would never notice.
_PROBE_BLOB = os.urandom(256 * 1024)


def _caller_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "")


@router.get("/ip", response_class=PlainTextResponse)
async def whats_my_ip(request: Request, _device: RouterDevice = Depends(require_device)) -> str:
    """Where the agent's probe came out: the router's ISP address when sent direct, the VPN
    node's address when sent through a server — the one fact that says whether VPN works."""
    return _caller_ip(request)


@router.get("/probe")
async def probe_blob(_device: RouterDevice = Depends(require_device)) -> Response:
    return Response(
        _PROBE_BLOB,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


def vpn_verdict(diagnostics: dict[str, Any] | None) -> dict[str, Any] | None:
    """Plain-language verdict from the agent's self-test (agent v3+), or None without one.

    ``ok``      — traffic through the balancer exits via a VPN server, big downloads pass;
    ``direct``  — the probe got out, but with the router's own address: every server failed and
                  the balancer fell back to direct (VPN silently off);
    ``slow``    — exits via VPN, but large transfers stall (ISP throttling to that server);
    ``fail``    — nothing passes through the balancer at all.
    """
    tests = (diagnostics or {}).get("self_test")
    if not isinstance(tests, list) or not tests:
        return None
    by_name = {str(t.get("name")): t for t in tests if isinstance(t, dict)}
    direct = by_name.get("direct") or {}
    balancer = by_name.get("balancer")
    servers = [t for name, t in by_name.items() if name.startswith("proxy-")]
    direct_ip = str(direct.get("ip") or "")
    if balancer is None:
        return {"state": "none", "text": "VPN-серверов в конфиге нет", "servers": servers}
    exit_ip = str(balancer.get("ip") or "")
    if not balancer.get("ok"):
        state, text = "fail", f"VPN не работает: {balancer.get('error') or 'нет ответа'}"
    elif direct_ip and exit_ip == direct_ip:
        state = "direct"
        text = (
            "VPN не работает: все серверы недоступны, трафик идёт напрямую "
            f"с адреса провайдера {direct_ip}"
        )
    elif balancer.get("big") and balancer.get("big") != "ok":
        state = "slow"
        text = (
            f"VPN подключается (выход {exit_ip}), но большие загрузки обрываются: {balancer['big']}"
        )
    else:
        state, text = "ok", f"VPN работает, выход через {exit_ip}"
    return {
        "state": state,
        "text": text,
        "exit_ip": exit_ip,
        "direct_ip": direct_ip,
        "servers": servers,
    }


async def _alert_on_vpn_change(
    container: AppContainer, device: RouterDevice, verdict: dict[str, Any] | None
) -> None:
    """One message to the «alerts» topic when a router's VPN breaks (confirmed by two reports
    in a row, so a restart's first seconds don't page anyone) and one when it recovers."""
    if verdict is None or verdict["state"] == "none":
        return
    key = f"agent:vpn:{device.id}"
    prev_raw = await _redis_get(container, key)
    prev = json.loads(prev_raw) if prev_raw else {"state": None, "bad": 0, "alerted": False}
    bad = verdict["state"] != "ok"
    streak = int(prev.get("bad") or 0) + 1 if bad else 0
    alerted = bool(prev.get("alerted"))
    text = None
    if bad and streak >= 2 and not alerted:
        text = f"🔴 Роутер «{device.label}»: {verdict['text']}"
        alerted = True
    elif not bad and alerted:
        text = f"🟢 Роутер «{device.label}»: {verdict['text']}"
        alerted = False
    await _redis_set(
        container,
        key,
        json.dumps({"state": verdict["state"], "bad": streak, "alerted": alerted}),
        7 * 86400,
    )
    if text:
        from src.infrastructure.services.reports import send_topic_report

        try:
            await send_topic_report(container, "alerts", text)
        except Exception:
            log.warning("agent: vpn alert not delivered", device_id=device.id)


@router.post("/heartbeat", status_code=204)
async def heartbeat(
    # Loose by design — the installer/agent script (stage 6/7) isn't finalized yet, and this
    # endpoint's only job is to store whatever the agent reports, not validate a fleet-wide
    # schema no device exists yet to have gotten wrong.
    body: dict[str, Any],
    device: RouterDevice = Depends(require_device),
    container: AppContainer = Depends(get_container),
) -> None:
    if not await _rate_limit_ok(container, f"agent:hb:{device.id}"):
        raise HTTPException(429, "too many requests")
    async with container.uow() as uow:
        fresh = await uow.router_devices.get(device.id)
        if fresh is None:
            return
        fresh.last_seen_at = dt.datetime.now(dt.UTC)
        xray_running = body.get("xray_running")
        fresh.status = (
            RouterDeviceStatus.OFFLINE if xray_running is False else RouterDeviceStatus.ONLINE
        )
        if body.get("xray_version") is not None:
            fresh.xray_version = str(body["xray_version"])[:32]
        if body.get("active_outbound") is not None:
            fresh.active_outbound = str(body["active_outbound"])[:64]
        if body.get("external_ip") is not None:
            fresh.external_ip = str(body["external_ip"])[:45]
        last_error = body.get("last_error")
        fresh.last_error = str(last_error)[:_LAST_ERROR_MAX] if last_error else None
        diagnostics = body.get("diagnostics")
        stored = isinstance(diagnostics, dict) and len(json.dumps(diagnostics)) <= _DIAGNOSTICS_MAX
        if stored:
            fresh.diagnostics = diagnostics
        await uow.commit()
    if stored:
        await _alert_on_vpn_change(container, fresh, vpn_verdict(diagnostics))


@router.post("/install-report", status_code=204)
async def install_report(
    body: dict[str, Any],
    device: RouterDevice = Depends(require_device),
    container: AppContainer = Depends(get_container),
) -> None:
    """Preflight/install result from the installer script (stage 7) — stored as-is so an admin
    can see what a technician's install actually did without visiting the router."""
    async with container.uow() as uow:
        fresh = await uow.router_devices.get(device.id)
        if fresh is None:
            return
        fresh.install_report = body
        if fresh.status is RouterDeviceStatus.PENDING:
            fresh.status = RouterDeviceStatus.ONLINE
        fresh.last_seen_at = dt.datetime.now(dt.UTC)
        await uow.commit()
