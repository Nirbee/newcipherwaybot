"""READ-ONLY probe: what routing rules does Remnawave hand a Happ client (Xray-JSON subscription)?

The router control plane wants to reuse exactly those rules (RU services direct, the rest via
proxy) instead of an admin maintaining a second copy. This fetches ONE router device's
subscription URL the way Happ does and prints only the routing section plus outbound
tags/protocols — never outbound settings (those carry the client's uuid/keys).

Usage (on the server):
  docker compose --env-file .env -f docker/compose.prod.yml exec -e PYTHONPATH=/app web \
      python scripts/probe_happ_routing.py [router_device_id]
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from typing import Any

import httpx

from src.infrastructure.di import AppContainer

_USER_AGENTS = ("Happ/3.7.0", "Happ/2.0.0")


def _describe(payload: Any) -> None:
    configs = payload if isinstance(payload, list) else [payload]
    print(f"configs in response: {len(configs)}")
    first = configs[0] if configs and isinstance(configs[0], dict) else {}
    print(f"top-level keys: {sorted(first.keys())}")
    outbounds = first.get("outbounds") or []
    print("outbounds (tag / protocol):")
    for ob in outbounds:
        if isinstance(ob, dict):
            print(f"  - {ob.get('tag')!r} / {ob.get('protocol')!r}")
    routing = first.get("routing")
    print("routing:")
    print(json.dumps(routing, ensure_ascii=False, indent=2))
    dns = first.get("dns")
    if dns is not None:
        print("dns:")
        print(json.dumps(dns, ensure_ascii=False, indent=2))


async def main(argv: list[str]) -> int:
    container = AppContainer.from_env()
    try:
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
        url = panel_user.subscription_url if panel_user else None
        if not url:
            print("panel user has no subscription URL")
            return 1
        print(f"router device #{device.id} ({device.label}) -> subscription #{sub.id}")

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            for ua in _USER_AGENTS:
                r = await http.get(url, headers={"User-Agent": ua})
                ctype = r.headers.get("content-type", "")
                print(f"\n=== User-Agent {ua}: HTTP {r.status_code}, content-type {ctype}")
                try:
                    payload = r.json()
                except ValueError:
                    print(f"not JSON (first 120 chars): {r.text[:120]!r}")
                    continue
                _describe(payload)
                break
    finally:
        await container.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
