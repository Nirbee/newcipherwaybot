"""Admin router-devices API: create/list/patch/rotate/revoke + eligible-hosts allowlist.

Reuses the ApiTestContainer/client fixture from test_admin_api.py (fakes + in-memory sqlite).
"""

from __future__ import annotations

import httpx

from src.application.dto.panel import PanelHost
from src.application.dto.pricing import PurchaseRequest
from src.core.enums import Currency
from tests.factories import make_plan, make_user
from tests.integration.test_admin_api import ApiTestContainer, _login, client  # noqa: F401


def _host(uuid: str, **overrides: object) -> PanelHost:
    base: dict[str, object] = {
        "uuid": uuid,
        "remark": f"Host {uuid}",
        "address": "203.0.113.1",
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


async def _make_subscription(container: ApiTestContainer, *, telegram_id: int = 555000) -> int:
    async with container.uow() as uow:
        user = await make_user(uow, telegram_id=telegram_id)
        plan, _ = await make_plan(uow, code=f"plan-{telegram_id}")
        await uow.commit()
        req = PurchaseRequest(
            user_id=user.id, plan_id=plan.id, duration_days=30, currency=Currency.RUB
        )
        sub = await container.subscriptions.grant(uow, user=user, plan=plan, req=req)
        user.current_subscription_id = sub.id
        await uow.commit()
        return sub.id


async def test_create_auto_assigns_least_loaded_host(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a"), _host("host-b")]
    sub_id = await _make_subscription(container, telegram_id=1)
    auth = await _login(http)

    res = await http.put(
        "/api/admin/routers/config/eligible-hosts",
        headers=auth,
        json={"host_uuids": ["host-a", "host-b"]},
    )
    assert res.status_code == 200

    res = await http.post(
        "/api/admin/routers", headers=auth, json={"subscription_id": sub_id, "label": "Client 1"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["primary_host_uuid"] in {"host-a", "host-b"}
    assert body["backup_host_uuid"] in {"host-a", "host-b"}
    assert body["primary_host_uuid"] != body["backup_host_uuid"]
    assert body["warning"] is None
    assert len(body["token"]) > 20
    first_primary = body["primary_host_uuid"]

    # second device should be steered to the OTHER host (least-connections)
    sub_id_2 = await _make_subscription(container, telegram_id=2)
    res = await http.post(
        "/api/admin/routers", headers=auth, json={"subscription_id": sub_id_2, "label": "Client 2"}
    )
    assert res.status_code == 200
    assert res.json()["primary_host_uuid"] != first_primary


async def test_create_without_eligible_hosts_warns_and_leaves_freedom_only(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a")]
    sub_id = await _make_subscription(container, telegram_id=3)
    auth = await _login(http)

    res = await http.post(
        "/api/admin/routers", headers=auth, json={"subscription_id": sub_id, "label": "No eligible"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["primary_host_uuid"] is None
    assert body["backup_host_uuid"] is None
    assert body["warning"] is not None


async def test_create_force_mode_requires_primary_host(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    sub_id = await _make_subscription(container, telegram_id=4)
    auth = await _login(http)

    res = await http.post(
        "/api/admin/routers",
        headers=auth,
        json={"subscription_id": sub_id, "label": "Forced", "mode": "force"},
    )
    assert res.status_code == 422

    res = await http.post(
        "/api/admin/routers",
        headers=auth,
        json={
            "subscription_id": sub_id, "label": "Forced", "mode": "force",
            "primary_host_uuid": "host-x",
        },
    )
    assert res.status_code == 200
    assert res.json()["primary_host_uuid"] == "host-x"


async def test_create_unknown_subscription_404s(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, _ = client
    auth = await _login(http)
    res = await http.post(
        "/api/admin/routers", headers=auth, json={"subscription_id": 999999, "label": "Ghost"}
    )
    assert res.status_code == 404


async def test_list_shows_subscription_label(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    sub_id = await _make_subscription(container, telegram_id=555000)
    auth = await _login(http)
    await http.post(
        "/api/admin/routers", headers=auth, json={"subscription_id": sub_id, "label": "R1"}
    )
    res = await http.get("/api/admin/routers", headers=auth)
    assert res.status_code == 200
    items = res.json()["items"]
    row = next(i for i in items if i["label"] == "R1")
    assert row["subscription_id"] == sub_id
    assert row["subscription_label"] == "id555000"
    assert row["status"] == "pending"
    assert row["is_online"] is False


async def test_get_detail_includes_available_hosts(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a"), _host("host-b", protocol="hysteria")]
    sub_id = await _make_subscription(container, telegram_id=5)
    auth = await _login(http)
    created = await http.post(
        "/api/admin/routers", headers=auth, json={"subscription_id": sub_id, "label": "R"}
    )
    device_id = created.json()["id"]

    res = await http.get(f"/api/admin/routers/{device_id}", headers=auth)
    assert res.status_code == 200
    body = res.json()
    # only the vless host is offered — hysteria is filtered
    assert [h["uuid"] for h in body["available_hosts"]] == ["host-a"]


async def test_patch_rotate_revoke_delete_lifecycle(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    sub_id = await _make_subscription(container, telegram_id=6)
    auth = await _login(http)
    created = await http.post(
        "/api/admin/routers", headers=auth, json={"subscription_id": sub_id, "label": "Old label"}
    )
    device_id = created.json()["id"]
    old_token = created.json()["token"]

    res = await http.patch(
        f"/api/admin/routers/{device_id}",
        headers=auth,
        json={"label": "New label", "note": "installed by tech A"},
    )
    assert res.status_code == 200
    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=auth)).json()
    assert detail["label"] == "New label"
    assert detail["note"] == "installed by tech A"

    res = await http.post(f"/api/admin/routers/{device_id}/rotate", headers=auth)
    assert res.status_code == 200
    new_token = res.json()["token"]
    assert new_token != old_token

    res = await http.post(f"/api/admin/routers/{device_id}/revoke", headers=auth)
    assert res.status_code == 200
    detail = (await http.get(f"/api/admin/routers/{device_id}", headers=auth)).json()
    assert detail["status"] == "revoked"

    res = await http.delete(f"/api/admin/routers/{device_id}", headers=auth)
    assert res.status_code == 200
    res = await http.get(f"/api/admin/routers/{device_id}", headers=auth)
    assert res.status_code == 404


async def test_eligible_hosts_round_trip(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    container.remnawave_client.hosts = [_host("host-a"), _host("host-b")]
    auth = await _login(http)

    res = await http.get("/api/admin/routers/config/eligible-hosts", headers=auth)
    assert res.status_code == 200
    assert all(not h["eligible"] for h in res.json()["items"])  # opt-in default: none eligible

    res = await http.put(
        "/api/admin/routers/config/eligible-hosts", headers=auth, json={"host_uuids": ["host-a"]}
    )
    assert res.status_code == 200

    res = await http.get("/api/admin/routers/config/eligible-hosts", headers=auth)
    items = {h["uuid"]: h["eligible"] for h in res.json()["items"]}
    assert items == {"host-a": True, "host-b": False}


async def test_tags_the_subscriptions_panel_user_as_router(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    sub_id = await _make_subscription(container, telegram_id=7)
    auth = await _login(http)
    await http.post(
        "/api/admin/routers", headers=auth, json={"subscription_id": sub_id, "label": "Tag test"}
    )
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(sub_id)
    panel_user = container.remnawave_client.users[sub.remnawave_uuid]
    assert panel_user.tag == "ROUTER"


async def test_routers_requires_admin_token(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, _ = client
    assert (await http.get("/api/admin/routers")).status_code == 401
