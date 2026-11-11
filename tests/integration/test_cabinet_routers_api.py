"""«Мой роутер» in the cabinet: only the owner's routers, «Напрямую», location pin, rate limits.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import datetime as dt
import json

import httpx

from src.application.services.router_config import location_id
from src.core.enums import RouterDeviceMode
from tests.integration.test_admin_api import ApiTestContainer, _tma_headers, client  # noqa: F401
from tests.integration.test_agent_api import _create_device, _host

TG_OWNER = 700100
TG_OTHER = 700200


def _hosts() -> list:
    return [
        _host("host-nl", remark="Нидерланды", address="203.0.113.1"),
        _host("host-de", remark="Германия", address="203.0.113.2"),
        _host("host-fi", remark="Финляндия", address="203.0.113.3"),
        _host("host-auto", remark="⚡ Автовыбор", address="203.0.113.4"),
        _host("host-hy", remark="Hysteria2", address="203.0.113.5", protocol="hysteria"),
    ]


async def _device(http: httpx.AsyncClient, container: ApiTestContainer) -> tuple[int, str]:
    container.remnawave_client.hosts = _hosts()
    return await _create_device(
        http,
        container,
        telegram_id=TG_OWNER,
        host_uuids=["host-nl", "host-de", "host-fi", "host-auto", "host-hy"],
    )


async def _mode(container: ApiTestContainer, device_id: int, mode: RouterDeviceMode) -> None:
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        device.mode = mode
        await uow.commit()


def _clear_limits(container: ApiTestContainer) -> None:
    for key in [k for k in container.redis.store if k.startswith("rl:router:")]:
        container.redis.store.pop(key)


async def test_list_shows_only_my_routers_and_supported_locations(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, _token = await _device(http, container)

    mine = (await http.get("/api/cabinet/routers", headers=_tma_headers(tg_id=TG_OWNER))).json()
    assert len(mine["items"]) == 1
    item = mine["items"][0]
    assert item["id"] == device_id
    assert item["vpn_mode"] == "vpn" and item["direct_until"] is None
    assert item["can_manage"] is True and item["can_choose_location"] is True
    assert item["location"]["auto"] is True
    # ⚡ Автовыбор and Hysteria never appear; locations carry an opaque id, no uuid or address.
    assert [loc["name"] for loc in item["locations"]] == ["Нидерланды", "Германия", "Финляндия"]
    assert item["locations"][0]["id"] == location_id("host-nl")
    assert "host-nl" not in json.dumps(item) and "203.0.113" not in json.dumps(item)
    assert "token" not in json.dumps(item).lower()

    other = (await http.get("/api/cabinet/routers", headers=_tma_headers(tg_id=TG_OTHER))).json()
    assert other == {"items": []}


async def test_foreign_or_missing_device_is_404(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, _token = await _device(http, container)
    stranger = _tma_headers(tg_id=TG_OTHER)
    await http.get("/api/cabinet/routers", headers=stranger)  # creates the stranger's account
    res = await http.post(
        f"/api/cabinet/routers/{device_id}/direct", headers=stranger, json={"duration": "1h"}
    )
    assert res.status_code == 404
    res = await http.post(
        f"/api/cabinet/routers/{device_id}/location", headers=stranger, json={"host": None}
    )
    assert res.status_code == 404
    missing = await http.post(
        "/api/cabinet/routers/999999/direct", headers=stranger, json={"duration": "1h"}
    )
    assert missing.status_code == 404


async def test_direct_durations_and_timer_expiry(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, _token = await _device(http, container)
    tma = _tma_headers(tg_id=TG_OWNER)
    url = f"/api/cabinet/routers/{device_id}/direct"

    res = await http.post(url, headers=tma, json={"duration": "1h"})
    assert res.status_code == 200 and res.json()["vpn_mode"] == "direct"
    assert res.json()["direct_forever"] is False

    assert (await http.post(url, headers=tma, json={"duration": "forever"})).status_code == 200
    item = (await http.get("/api/cabinet/routers", headers=tma)).json()["items"][0]
    assert item["direct_forever"] is True and item["vpn_mode"] == "direct"

    # «today» needs a timezone; a nonsense offset is refused, a real one lands within 24 h.
    assert (await http.post(url, headers=tma, json={"duration": "today"})).status_code == 400
    bad = await http.post(url, headers=tma, json={"duration": "today", "tz_offset_minutes": 9999})
    assert bad.status_code == 422
    res = await http.post(url, headers=tma, json={"duration": "today", "tz_offset_minutes": 180})
    until = dt.datetime.fromisoformat(res.json()["direct_until"])
    assert until <= dt.datetime.now(dt.UTC) + dt.timedelta(hours=24)

    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        device.direct_until = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        await uow.commit()
    item = (await http.get("/api/cabinet/routers", headers=tma)).json()["items"][0]
    assert item["vpn_mode"] == "vpn"  # the past moment reads as off

    off = await http.post(url, headers=tma, json={"duration": "off"})
    assert off.status_code == 200 and off.json()["vpn_mode"] == "vpn"


async def test_direct_limit_and_off_still_works(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, _token = await _device(http, container)
    tma = _tma_headers(tg_id=TG_OWNER)
    url = f"/api/cabinet/routers/{device_id}/direct"
    for _ in range(20):
        assert (await http.post(url, headers=tma, json={"duration": "1h"})).status_code == 200
    over = await http.post(url, headers=tma, json={"duration": "1h"})
    assert over.status_code == 429 and "частые" in over.json()["detail"]
    # The hour's allowance for «on» must never trap a home with VPN switched off.
    assert (await http.post(url, headers=tma, json={"duration": "off"})).status_code == 200


async def test_location_pin_validation_and_limit(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, token = await _device(http, container)
    tma = _tma_headers(tg_id=TG_OWNER)
    url = f"/api/cabinet/routers/{device_id}/location"

    # Not a listed location (unknown id, and ⚡ Автовыбор which is excluded) -> 400.
    assert (await http.post(url, headers=tma, json={"host": "nope"})).status_code == 400
    bad = await http.post(url, headers=tma, json={"host": location_id("host-auto")})
    assert bad.status_code == 400

    de = location_id("host-de")
    res = await http.post(url, headers=tma, json={"host": de})
    assert res.status_code == 200
    item = (await http.get("/api/cabinet/routers", headers=tma)).json()["items"][0]
    assert item["location"] == {"auto": False, "id": de, "name": "Германия"}
    assert item["applied"] is False  # the agent hasn't taken it yet

    # /config makes it the primary and reports it delivered; the backup is automatic and ≠ pin.
    cfg = await http.get("/api/agent/config", headers={"Authorization": f"Bearer {token}"})
    proxies = [o["tag"] for o in cfg.json()["outbounds"] if o["tag"].startswith("proxy-")]
    assert proxies[0] == "proxy-host-de"[:14]
    assert len(proxies) == 2 and len(set(proxies)) == 2
    item = (await http.get("/api/cabinet/routers", headers=tma)).json()["items"][0]
    assert item["applied"] is True

    # Repeating the current choice restarts nothing and costs no allowance.
    assert (await http.post(url, headers=tma, json={"host": de})).status_code == 200

    # Six changes an hour, then a clear 429.
    _clear_limits(container)
    for host_id in (None, de, None, de, None, de):
        assert (await http.post(url, headers=tma, json={"host": host_id})).status_code == 200
    over = await http.post(url, headers=tma, json={"host": None})
    assert over.status_code == 429 and "6" in over.json()["detail"]


async def test_location_rejected_outside_squads_and_on_force_routers(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, _token = await _device(http, container)
    tma = _tma_headers(tg_id=TG_OWNER)
    url = f"/api/cabinet/routers/{device_id}/location"

    # The customer's squads don't include this host's squad.
    container.remnawave_client.hosts = [
        _host("host-nl", remark="Нидерланды", address="203.0.113.1", squad_uuids=("squad-x",)),
    ]
    panel_uuid = next(iter(container.remnawave_client.users))
    import dataclasses

    container.remnawave_client.users[panel_uuid] = dataclasses.replace(
        container.remnawave_client.users[panel_uuid], internal_squads=("squad-y",)
    )
    res = await http.post(url, headers=tma, json={"host": location_id("host-nl")})
    assert res.status_code == 400

    # FORCE: the admin decides — the list and the choice are hidden, direct stays available.
    await _mode(container, device_id, RouterDeviceMode.FORCE)
    res = await http.post(url, headers=tma, json={"host": None})
    assert res.status_code == 409
    item = (await http.get("/api/cabinet/routers", headers=tma)).json()["items"][0]
    assert item["can_choose_location"] is False and item["locations"] == []
    direct = await http.post(
        f"/api/cabinet/routers/{device_id}/direct", headers=tma, json={"duration": "1h"}
    )
    assert direct.status_code == 200


async def test_management_needs_an_active_subscription(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    device_id, _token = await _device(http, container)
    tma = _tma_headers(tg_id=TG_OWNER)
    async with container.uow() as uow:
        device = await uow.router_devices.get(device_id)
        sub = await uow.subscriptions.get(device.subscription_id)
        sub.expire_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        await uow.commit()
    item = (await http.get("/api/cabinet/routers", headers=tma)).json()["items"][0]
    assert item["can_manage"] is False
    res = await http.post(
        f"/api/cabinet/routers/{device_id}/direct", headers=tma, json={"duration": "1h"}
    )
    assert res.status_code == 403
    res = await http.post(
        f"/api/cabinet/routers/{device_id}/location", headers=tma, json={"host": None}
    )
    assert res.status_code == 403
