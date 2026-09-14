"""STRICTLY READ-ONLY panel check. Issues ONLY GET requests — never writes.

Verifies auth + connection + version + field mapping against a live Remnawave panel
WITHOUT creating, editing, revoking or deleting anything. Safe to run against production.

Run: PYTHONPATH=. .venv/bin/python scripts/check_panel.py
Prints HTTP statuses and field *names* only (no values) so no user data / links leak.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from src.core.config import get_settings
from src.infrastructure.remnawave.client import _to_panel_user
from src.infrastructure.remnawave.connection import build_profile

# The ONLY HTTP method this script is allowed to use.
_ALLOWED = "GET"


async def GET(
    client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    assert _ALLOWED == "GET"  # hard guard: this script never writes
    return await client.request(_ALLOWED, path, params=params)


def _summarize(data: Any) -> str:
    resp = data.get("response", data) if isinstance(data, dict) else data
    if isinstance(resp, dict):
        # unwrap a common {response: {users: [...]}} shape
        for key in ("users", "internalSquads", "nodes", "items", "data", "hosts", "configProfiles"):
            if isinstance(resp.get(key), list):
                lst = resp[key]
                first = sorted(lst[0].keys()) if lst and isinstance(lst[0], dict) else None
                return f"list[{key}] count={len(lst)} first_keys={first}"
        return f"keys={sorted(resp.keys())}"
    if isinstance(resp, list):
        first = sorted(resp[0].keys()) if resp and isinstance(resp[0], dict) else None
        return f"list count={len(resp)} first_keys={first}"
    return f"scalar={type(resp).__name__}"


def _summarize_nested(item: dict[str, Any], *, depth: int = 1) -> dict[str, Any]:
    """Field NAMES/shapes only, `depth` levels into any object/list-of-objects field — never
    values, so Reality/stream-settings shapes (pbk/sid/sni/fingerprint/flow/network) can be
    inspected without printing secrets (private keys, short ids, client uuids) to a log."""
    out: dict[str, Any] = {}
    for key, val in item.items():
        if isinstance(val, dict):
            out[key] = _summarize_nested(val, depth=depth - 1) if depth > 0 else sorted(val.keys())
        elif isinstance(val, list):
            if val and isinstance(val[0], dict):
                shape = _summarize_nested(val[0], depth=depth - 1) if depth > 0 else sorted(val[0].keys())
                out[key] = f"list[{len(val)}] of {shape}"
            else:
                out[key] = f"list[{len(val)}]"
        else:
            out[key] = type(val).__name__
    return out


async def main() -> int:
    s = get_settings()
    profile = build_profile(s.remnawave)
    print(
        f"base_url={profile.base_url} verify={profile.verify} "
        f"has_auth={'Authorization' in profile.headers} has_secret_cookie={bool(profile.cookies)}"
    )

    checks = [
        ("system/health", "/api/system/health", None),
        ("system/metadata (версия)", "/api/system/metadata", None),
        ("system/stats", "/api/system/stats", None),
        ("internal-squads", "/api/internal-squads", None),
        ("nodes", "/api/nodes", None),
        ("hosts", "/api/hosts", None),
        ("config-profiles", "/api/config-profiles", None),
        ("users (1 record, keys only)", "/api/users", {"size": 1, "start": 0}),
    ]
    async with httpx.AsyncClient(
        base_url=profile.base_url,
        headers=profile.headers,
        cookies=profile.cookies,
        verify=profile.verify,
        timeout=20.0,
        follow_redirects=False,
    ) as client:
        for name, path, params in checks:
            try:
                r = await GET(client, path, params)
            except Exception as exc:
                print(f"[{name:32}] ERROR {type(exc).__name__}: {exc}")
                continue
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and "json" in ctype:
                try:
                    print(f"[{name:32}] 200  {_summarize(r.json())}")
                except Exception:
                    print(f"[{name:32}] 200  (non-JSON body)")
            else:
                snippet = "" if "json" in ctype else r.text[:80].replace("\n", " ")
                print(f"[{name:32}] {r.status_code}  ctype={ctype.split(';')[0]} {snippet}")

        # Host shape: field names one level deep (Reality/stream settings are almost certainly
        # nested) — needed to write the get_hosts() parser without guessing at the real shape.
        try:
            r = await GET(client, "/api/hosts")
            data = r.json()
            resp = data.get("response", data)
            hosts = resp.get("hosts") if isinstance(resp, dict) else resp
            if hosts:
                print(f"[hosts[0] shape (names only)  ] {_summarize_nested(dict(hosts[0]), depth=2)}")
            else:
                print("[hosts[0] shape               ] (empty list — no hosts configured?)")
        except Exception as exc:
            print(f"[hosts[0] shape               ] ERROR {type(exc).__name__}: {exc}")

        # Hosts only reference an inbound by uuid (inbound.configProfileUuid +
        # configProfileInboundUuid) — the actual Xray inbound (protocol/flow/streamSettings/
        # realitySettings: publicKey, shortIds, network) lives on the config profile, a
        # separate resource. Go several levels deep since Xray inbound JSON nests: profile ->
        # inbounds[] -> streamSettings -> realitySettings/xhttpSettings/grpcSettings.
        try:
            r = await GET(client, "/api/config-profiles")
            data = r.json()
            resp = data.get("response", data)
            profiles = None
            for key in ("configProfiles", "profiles", "items"):
                if isinstance(resp.get(key), list) if isinstance(resp, dict) else False:
                    profiles = resp[key]
                    break
            if profiles is None and isinstance(resp, list):
                profiles = resp
            if profiles:
                print(
                    "[config-profiles[0] shape (names only)] "
                    f"{_summarize_nested(dict(profiles[0]), depth=4)}"
                )
            else:
                print(f"[config-profiles shape        ] no list found, top keys={sorted(resp.keys()) if isinstance(resp, dict) else type(resp).__name__}")
        except Exception as exc:
            print(f"[config-profiles shape        ] ERROR {type(exc).__name__}: {exc}")

        # Squad -> host membership: hosts only carry `excludedInternalSquads` (a blocklist), so
        # membership must be defined from the squad side via its `inbounds` field. Values here
        # are just uuids/refs (not secrets), so print them for real to nail the join key exactly.
        try:
            r = await GET(client, "/api/internal-squads")
            data = r.json()
            resp = data.get("response", data)
            squads = resp.get("internalSquads") if isinstance(resp, dict) else resp
            if squads:
                sq = dict(squads[0])
                inbounds = sq.get("inbounds")
                print(
                    "[internal-squads[0].inbounds  ] "
                    f"type={type(inbounds).__name__} value={inbounds!r}"
                )
            else:
                print("[internal-squads[0].inbounds  ] (no squads)")
        except Exception as exc:
            print(f"[internal-squads[0].inbounds  ] ERROR {type(exc).__name__}: {exc}")

        # Mapping verification: run one real user through our DTO mapper (flags only, no values).
        try:
            r = await GET(client, "/api/users", {"size": 1, "start": 0})
            data = r.json()
            resp = data.get("response", data)
            users = resp.get("users") if isinstance(resp, dict) else resp
            if users:
                m = _to_panel_user(dict(users[0]))
                print(
                    "[mapping _to_panel_user       ] "
                    f"short_id={bool(m.short_id)} status_enabled={m.is_enabled} "
                    f"expire={m.expire_at is not None} telegram={m.telegram_id is not None} "
                    f"sub_url={bool(m.subscription_url)} squads={len(m.internal_squads)} "
                    f"limit_bytes={m.traffic_limit_bytes} used_bytes={m.traffic_used_bytes}"
                )
        except Exception as exc:
            print(f"[mapping _to_panel_user       ] ERROR {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
