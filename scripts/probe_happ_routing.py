"""READ-ONLY probe: where do the Xray-JSON routing rules Happ receives live, and what are they?

The router control plane wants to reuse exactly those rules (RU services direct, the rest via
proxy) instead of an admin maintaining a second copy. Tries, in order:
  1. the panel's subscription-templates API (where the Xray-JSON template is edited);
  2. one router device's subscription URL with explicit client-type suffixes.
Prints only routing/dns sections, outbound tags/protocols and masked link summaries — never
outbound settings or uuids (those are the client's credentials).

Usage (on the server):
  docker compose --env-file .env -f docker/compose.prod.yml exec -e PYTHONPATH=/app web \
      python scripts/probe_happ_routing.py [router_device_id]
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
from dataclasses import replace
from typing import Any
from urllib.parse import unquote

import httpx

from src.infrastructure.di import AppContainer

_UA = "Happ/3.7.0"
_TEMPLATE_PATHS = ("/api/subscription-templates", "/api/subscription-templates/XRAY_JSON")
_SUB_SUFFIXES = ("/json", "/v2ray-json", "/xray-json")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _dump(title: str, value: Any) -> None:
    print(f"{title}:")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _describe_config(payload: Any) -> None:
    configs = payload if isinstance(payload, list) else [payload]
    print(f"configs in response: {len(configs)}")
    first = configs[0] if configs and isinstance(configs[0], dict) else {}
    print(f"top-level keys: {sorted(first.keys())}")
    for ob in first.get("outbounds") or []:
        if isinstance(ob, dict):
            print(f"  outbound {ob.get('tag')!r} / {ob.get('protocol')!r}")
    _dump("routing", first.get("routing"))
    if first.get("dns") is not None:
        _dump("dns", first.get("dns"))


def _peek_base64(text: str) -> None:
    try:
        decoded = base64.b64decode(text.strip() + "=" * (-len(text.strip()) % 4)).decode(
            "utf-8", "replace"
        )
    except ValueError:
        print("  (not base64)")
        return
    for line in decoded.splitlines()[:15]:
        masked = _UUID_RE.sub("<uuid>", line)
        head, _, remark = masked.partition("#")
        head = head.split("?", 1)[0]
        print(f"  {head}  #{unquote(remark)}")


async def _probe_templates(http: httpx.AsyncClient) -> None:
    for path in _TEMPLATE_PATHS:
        r = await http.get(path)
        print(f"\n=== panel GET {path}: HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  body: {r.text[:200]!r}")
            continue
        data = r.json()
        data = data.get("response", data) if isinstance(data, dict) else data
        items = data
        if isinstance(data, dict):
            items = data.get("templates") or data.get("items") or [data]
        for tpl in items if isinstance(items, list) else []:
            if not isinstance(tpl, dict):
                continue
            ttype = str(tpl.get("templateType") or tpl.get("type") or "")
            print(f"  template name={tpl.get('name')!r} type={ttype!r} keys={sorted(tpl.keys())}")
            if "XRAY" not in ttype.upper() or "JSON" not in ttype.upper():
                continue
            body = tpl.get("templateJson") or tpl.get("templateJSON") or tpl.get("json")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except ValueError:
                    print("  templateJson is a non-JSON string")
                    continue
            if isinstance(body, dict):
                print(f"  templateJson top-level keys: {sorted(body.keys())}")
                _dump("  routing", body.get("routing"))
                if body.get("dns") is not None:
                    _dump("  dns", body.get("dns"))


async def main(argv: list[str]) -> int:
    container = AppContainer.from_env()
    try:
        panel_http: httpx.AsyncClient = container.remnawave_client._http
        await _probe_templates(panel_http)

        async with container.uow() as uow:
            if argv:
                device = await uow.router_devices.get(int(argv[0]))
            else:
                devices = await uow.router_devices.list(limit=1)
                device = devices[0] if devices else None
            if device is None:
                print("no router device found")
                return 1
            sub = await uow.subscriptions.get(device.subscription_id)
            owner = await uow.users.get(sub.user_id) if sub else None
        if sub is None or sub.panel_ref is None:
            print("device's subscription has no panel link")
            return 1
        ref = replace(sub.panel_ref, telegram_id=owner.telegram_id if owner else None)
        panel_user = await container.remnawave_client.get_user(ref)
        url = (panel_user.subscription_url if panel_user else None) or ""
        if not url:
            print("panel user has no subscription URL")
            return 1
        print(f"\nrouter device #{device.id} ({device.label}) -> subscription #{sub.id}")

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            r = await http.get(url, headers={"User-Agent": _UA})
            print(f"\n=== plain URL, UA {_UA}: HTTP {r.status_code} — links (uuids masked):")
            _peek_base64(r.text)
            for suffix in _SUB_SUFFIXES:
                r = await http.get(url.rstrip("/") + suffix, headers={"User-Agent": _UA})
                ctype = r.headers.get("content-type", "")
                print(f"\n=== URL + {suffix}: HTTP {r.status_code}, content-type {ctype}")
                try:
                    payload = r.json()
                except ValueError:
                    print(f"  not JSON: {r.text[:80]!r}")
                    continue
                _describe_config(payload)
                break
    finally:
        await container.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
