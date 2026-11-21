"""LAN Play API (/api/app/v1/lan): switches, limits, rooms, the policy kept in step with the DB
(and confirmed before any key), kick + ban, opaque names, the cleanup and the admin screen.

Reuses the ASGI-client fixture (fakes + in-memory sqlite) and the subscription helper from
test_app_api.py; Headscale is the in-memory FakeHeadscale.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from src.application.services.lan import is_lan_user
from src.core.enums import SubscriptionStatus, UserStatus
from src.infrastructure.database.models.lan_room import LanRoom, LanRoomBan, LanRoomMember
from src.infrastructure.services import lan as lan_service
from src.infrastructure.taskiq import tasks
from tests.fakes.headscale import FakeHeadscale
from tests.integration.test_admin_api import ApiTestContainer, _login, _tma_headers, client
from tests.integration.test_app_api import _h, _sub

_FIXTURES = (client,)  # the shared ASGI-client fixture, re-exported (keeps ruff F401/F811 quiet)

Client = tuple[httpx.AsyncClient, ApiTestContainer]
BASE = "/api/app/v1/lan"
NO_STATS = {"removed": 0, "policy_pushed": 1, "expired_nodes": 0, "deleted": 0, "renamed": 0}


async def _lan(container: ApiTestContainer, **overrides: Any) -> FakeHeadscale:
    """Switch LAN on with a configured server + key; Headscale = a fresh fake."""
    hs = FakeHeadscale()
    container.headscale_factory = hs.factory  # type: ignore[attr-defined]
    values = {"LAN_ENABLED": True, "LAN_HEADSCALE_API_KEY": "hs-secret-key", **overrides}
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, values)
        await uow.commit()
    return hs


async def _hs_user(container: ApiTestContainer, sub_id: int) -> str:
    async with container.uow() as uow:
        name = await uow.lan_accounts.hs_user_of(sub_id)
    assert name is not None
    return name


def _acl_users(hs: FakeHeadscale) -> list[set[str]]:
    return [{s.rstrip("@") for s in acl["src"]} for acl in json.loads(hs.policy)["acls"]]


def _fresh(container: ApiTestContainer) -> None:
    """Drop the 5 s node-list cache (the fake Redis doesn't model expiry)."""
    container.redis.store.pop(lan_service.NODES_CACHE_KEY, None)


async def _player(container: ApiTestContainer, n: int, **kw: Any) -> tuple[dict[str, str], int]:
    token = f"LanTok{n:08d}"
    _, sub_id = await _sub(container, token, tg=6600000 + n, **kw)
    return _h(token), sub_id


async def _create(http: httpx.AsyncClient, h: dict[str, str], name: str = "Майн") -> Any:
    res = await http.post(f"{BASE}/rooms", headers=h, json={"name": name})
    assert res.status_code == 200, res.text
    return res.json()


async def _join(http: httpx.AsyncClient, h: dict[str, str], code: str) -> httpx.Response:
    return await http.post(f"{BASE}/rooms/join", headers=h, json={"code": code})


async def _members(container: ApiTestContainer) -> list[tuple[int, int]]:
    async with container.uow() as uow:
        return [(m.room_id, m.subscription_id) for m in await uow.lan_members.list()]


async def _expire(container: ApiTestContainer, sub_id: int) -> None:
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(sub_id)
        assert sub is not None
        sub.status = SubscriptionStatus.EXPIRED
        await uow.commit()


# --- switches and gates --------------------------------------------------------------------
async def test_lan_disabled(client: Client) -> None:
    http, container = client
    h, _ = await _player(container, 1)
    res = await http.get(f"{BASE}/status", headers=h)
    assert res.status_code == 200
    assert res.json() == {
        "enabled": False,
        "login_server": None,
        "max_room_members": 8,
        "max_rooms_owned": 3,
        "max_rooms_joined": 10,
        "max_devices": 5,
    }
    for method, path in [("get", "/rooms"), ("post", "/device"), ("get", "/devices")]:
        kwargs: dict[str, Any] = {"json": {}} if method == "post" else {}
        r = await getattr(http, method)(f"{BASE}{path}", headers=h, **kwargs)
        assert (r.status_code, r.json()) == (404, {"detail": "lan_disabled"})


async def test_status_and_no_token(client: Client) -> None:
    http, container = client
    await _lan(container)
    h, _ = await _player(container, 2)
    body = (await http.get(f"{BASE}/status", headers=h)).json()
    assert body["enabled"] is True and body["login_server"] == "https://lan.cipherway.ru"
    assert (await http.get(f"{BASE}/status")).status_code == 401


@pytest.mark.parametrize("expired_by", ["status", "date"])
async def test_inactive_subscription_is_403(client: Client, expired_by: str) -> None:
    http, container = client
    await _lan(container)
    if expired_by == "status":
        h, _ = await _player(container, 3, status=SubscriptionStatus.EXPIRED)
    else:
        h, sub_id = await _player(container, 3)
        async with container.uow() as uow:
            sub = await uow.subscriptions.get(sub_id)
            assert sub is not None
            sub.expire_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
            await uow.commit()
    for res in [
        await http.get(f"{BASE}/status", headers=h),
        await http.post(f"{BASE}/device", headers=h, json={"hostname": "pc"}),
        await http.post(f"{BASE}/rooms", headers=h, json={"name": "x"}),
        await _join(http, h, "ABCDEF"),
    ]:
        assert (res.status_code, res.json()) == (403, {"detail": "subscription_inactive"})


# --- devices ---------------------------------------------------------------------------------
async def test_device_key_hostname_and_opaque_names(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h, sub_id = await _player(container, 4)
    res = await http.post(f"{BASE}/device", headers=h, json={"hostname": "DESKTOP-VASYA"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"login_server", "auth_key", "expires_at", "hostname"}
    assert body["login_server"] == "https://lan.cipherway.ru"
    assert body["auth_key"] == "hskey-1"
    assert body["hostname"].startswith("cw-") and len(body["hostname"]) == 9
    expires = dt.datetime.fromisoformat(body["expires_at"])
    assert dt.timedelta(minutes=9) < expires - dt.datetime.now(dt.UTC) <= dt.timedelta(minutes=10)
    name = await _hs_user(container, sub_id)
    assert is_lan_user(name) and f"sub-{sub_id}" != name
    assert hs.keys[0]["user"] == hs.users[name].id  # the key is for THIS subscription's user
    assert hs.opened[0] == ("https://lan.cipherway.ru", "hs-secret-key")
    # the app logs in; nothing a peer sees in the netmap carries a subscription id or PC name
    hs.register(body["auth_key"], name=body["hostname"])
    assert all("sub-" not in u for u in hs.users)
    assert all(n.name.startswith("cw-") for n in hs.nodes.values())
    assert "sub-" not in hs.policy and json.loads(hs.policy) == {"acls": [], "grants": []}


async def test_device_keys_cannot_exceed_the_limit(client: Client) -> None:
    http, container = client
    hs = await _lan(container, LAN_MAX_DEVICES=2)
    h, sub_id = await _player(container, 5)
    # asking again and again never leaves more than one usable key around
    for _ in range(4):
        assert (await http.post(f"{BASE}/device", headers=h, json={})).status_code == 200
    assert hs.pending_keys() == 1
    first = (await http.post(f"{BASE}/device", headers=h, json={})).json()
    hs.register(first["auth_key"], name=first["hostname"])
    name = await _hs_user(container, sub_id)
    hs.add_node(name, name="cw-0000aa", expiry=dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
    second = (await http.post(f"{BASE}/device", headers=h, json={})).json()  # 1 live (+1 expired)
    hs.register(second["auth_key"], name=second["hostname"])
    res = await http.post(f"{BASE}/device", headers=h, json={})
    assert (res.status_code, res.json()) == (409, {"detail": "device_limit"})
    assert hs.pending_keys() == 0  # the refused request left no key behind either
    live = [n for n in hs.nodes.values() if n.user == name and not n.is_expired()]
    assert len(live) == 2

    devices = (await http.get(f"{BASE}/devices", headers=h)).json()
    assert len(devices) == 3
    assert sum(d["expired"] for d in devices) == 1
    assert set(devices[0]) == {"node_id", "name", "ip", "online", "last_seen", "expired"}
    # freeing a slot lets the next device in
    await http.delete(f"{BASE}/device/{live[0].id}", headers=h)
    assert (await http.post(f"{BASE}/device", headers=h, json={})).status_code == 200


async def test_file_mode_headscale_gets_no_key(client: Client) -> None:
    """Fail closed: a server that can't take our policy (policy.mode: file, i.e. possibly
    allow-all) never gets a device registered."""
    http, container = client
    hs = await _lan(container)
    h, _ = await _player(container, 6)
    hs.file_mode = True
    res = await http.post(f"{BASE}/device", headers=h, json={})
    assert (res.status_code, res.json()) == (503, {"detail": "lan_unavailable"})
    assert hs.keys == []
    hs.file_mode = False
    hs.down = True
    assert (await http.post(f"{BASE}/device", headers=h, json={})).status_code == 503
    assert hs.keys == []


async def test_device_repairs_a_drifted_policy_first(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, s1 = await _player(container, 7)
    await _create(http, h1)
    hs.policy = "{}"  # someone reset it to allow-all
    assert (await http.post(f"{BASE}/device", headers=h1, json={})).status_code == 200
    assert _acl_users(hs) == [{await _hs_user(container, s1)}]


async def test_devices_of_new_subscription_and_foreign_delete(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, _ = await _player(container, 8)
    h2, s2 = await _player(container, 9)
    assert (await http.get(f"{BASE}/devices", headers=h1)).json() == []  # no account yet
    await http.post(f"{BASE}/device", headers=h2, json={})
    foreign = hs.add_node(await _hs_user(container, s2))
    res = await http.delete(f"{BASE}/device/{foreign.id}", headers=h1)
    assert (res.status_code, res.json()) == (404, {"detail": "device_not_found"})
    assert foreign.id in hs.nodes
    assert (await http.delete(f"{BASE}/device/{foreign.id}", headers=h2)).status_code == 200
    assert foreign.id not in hs.nodes


# --- rooms -----------------------------------------------------------------------------------
async def test_create_join_and_policy(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, s1 = await _player(container, 10, email="owner@example.com", username="vasya_tg")
    h2, s2 = await _player(container, 11, email="friend@example.com", username="petya_tg")
    h3, s3 = await _player(container, 12)

    room = await _create(http, h1, "  Наш ‮  сервер​ ")
    assert room["name"] == "Наш сервер" and room["is_owner"] is True
    code = room["code"]
    assert len(code) == 6
    u1 = await _hs_user(container, s1)
    assert _acl_users(hs) == [{u1}]

    res = await _join(http, h2, code.lower())
    assert res.status_code == 200, res.text
    u2 = await _hs_user(container, s2)
    hs.add_node(u2, name="DESKTOP-PETYA", ip="100.64.0.50", online=False)
    hs.add_node(u2, name="cw-0a1b2c-x7", ip="100.64.0.51")
    _fresh(container)
    joined = (await http.get(f"{BASE}/rooms", headers=h2)).json()[0]
    assert joined["id"] == room["id"] and joined["is_owner"] is False
    assert joined["code"] is None  # only the owner sees (and can share) the code
    assert [(m["label"], m["is_self"]) for m in joined["members"]] == [
        ("Игрок 1", False),
        ("Игрок 2", True),
    ]
    assert joined["members"][1]["devices"] == [
        {"name": "устройство", "ip": "100.64.0.50", "online": False},  # not our name → hidden
        {"name": "cw-0a1b2c", "ip": "100.64.0.51", "online": True},
    ]
    assert _acl_users(hs) == [{u1, u2}]
    again = await _join(http, h2, code)  # joining again is a no-op, not a second seat
    assert len(again.json()["members"]) == 2

    # labels / member ids / policy carry no personal data or subscription ids
    listing = await http.get(f"{BASE}/rooms", headers=h1)
    for secret in ("owner@example.com", "friend@example.com", "vasya", "petya", "PETYA", "6600010"):
        assert secret not in listing.text
    assert listing.json()[0]["code"] == code
    assert set(listing.json()[0]["members"][0]) == {"member_id", "label", "devices", "is_self"}
    assert "sub-" not in hs.policy

    # a second room is isolated from the first
    await _create(http, h3, "Другая")
    u3 = await _hs_user(container, s3)
    acls = _acl_users(hs)
    assert {u3} in acls and len(acls) == 2
    assert not any(u3 in a and u1 in a for a in acls)


async def test_unknown_code_full_room_and_limits(client: Client) -> None:
    http, container = client
    await _lan(container, LAN_MAX_ROOM_MEMBERS=2, LAN_MAX_ROOMS_OWNED=1)
    h1, _ = await _player(container, 13)
    h2, _ = await _player(container, 14)
    h3, _ = await _player(container, 15)
    room = await _create(http, h1)
    for code in ("ZZZZZZ", "bad", ""):
        res = await _join(http, h2, code)
        assert (res.status_code, res.json()) == (404, {"detail": "room_not_found"})
    assert (await _join(http, h2, room["code"])).status_code == 200
    res = await _join(http, h3, room["code"])
    assert (res.status_code, res.json()) == (409, {"detail": "room_full"})
    res = await http.post(f"{BASE}/rooms", headers=h1, json={"name": "ещё"})
    assert (res.status_code, res.json()) == (409, {"detail": "room_limit"})
    for bad in ("   ", "​‮"):
        res = await http.post(f"{BASE}/rooms", headers=h1, json={"name": bad})
        assert res.status_code == 422


async def test_membership_limit(client: Client) -> None:
    http, container = client
    await _lan(container, LAN_MAX_ROOMS_JOINED=2)
    hj, _ = await _player(container, 16)
    codes = []
    for n in range(3):
        h, _ = await _player(container, 17 + n)
        codes.append((await _create(http, h, f"R{n}"))["code"])
    assert (await _join(http, hj, codes[0])).status_code == 200
    assert (await _join(http, hj, codes[1])).status_code == 200
    res = await _join(http, hj, codes[2])
    assert (res.status_code, res.json()) == (409, {"detail": "membership_limit"})
    res = await http.post(f"{BASE}/rooms", headers=hj, json={"name": "своя"})
    assert (res.status_code, res.json()) == (409, {"detail": "membership_limit"})


async def test_owner_only_actions_and_kick_sticks(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, _ = await _player(container, 20)
    h2, _ = await _player(container, 21)
    h3, s3 = await _player(container, 22)
    room = await _create(http, h1)
    rid = room["id"]
    for h in (h2, h3):
        await _join(http, h, room["code"])
    members = (await http.get(f"{BASE}/rooms", headers=h1)).json()[0]["members"]
    third = members[2]["member_id"]
    u3 = await _hs_user(container, s3)

    # a member who isn't the owner can't kick or rotate the code
    res = await http.post(f"{BASE}/rooms/{rid}/kick", headers=h2, json={"member_id": third})
    assert (res.status_code, res.json()) == (403, {"detail": "not_owner"})
    res = await http.post(f"{BASE}/rooms/{rid}/regenerate-code", headers=h2)
    assert (res.status_code, res.json()) == (403, {"detail": "not_owner"})
    # an outsider doesn't even learn the room exists
    h4, _ = await _player(container, 23)
    res = await http.post(f"{BASE}/rooms/{rid}/kick", headers=h4, json={"member_id": third})
    assert (res.status_code, res.json()) == (404, {"detail": "room_not_found"})
    assert len(await _members(container)) == 3

    # the owner kicks: gone from the room AND the policy, the code rotates, a ban is recorded
    res = await http.post(f"{BASE}/rooms/{rid}/kick", headers=h1, json={"member_id": third})
    assert res.status_code == 200 and len(res.json()["members"]) == 2
    new_code = res.json()["code"]
    assert new_code != room["code"]
    assert all(u3 not in a for a in _acl_users(hs))
    for code in (room["code"], new_code):  # neither the old nor even the new code lets them in
        res = await _join(http, h3, code)
        assert res.status_code in (403, 404)
    assert (await _join(http, h3, new_code)).json() == {"detail": "banned"}
    assert (await _join(http, h4, new_code)).status_code == 200  # others still can
    res = await http.post(
        f"{BASE}/rooms/{rid}/kick", headers=h1, json={"member_id": members[0]["member_id"]}
    )
    assert res.status_code == 400

    # rotate the code: the old one stops working, no policy PUT is needed
    puts = hs.policy_puts
    res = await http.post(f"{BASE}/rooms/{rid}/regenerate-code", headers=h1)
    assert res.status_code == 200 and res.json()["code"] != new_code
    assert hs.policy_puts == puts
    assert (await _join(http, h4, new_code)).status_code == 404  # the previous code is dead


async def test_leave(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, _ = await _player(container, 24)
    h2, s2 = await _player(container, 25)
    room = await _create(http, h1)
    await _join(http, h2, room["code"])
    u2 = await _hs_user(container, s2)
    res = await http.post(f"{BASE}/rooms/{room['id']}/leave", headers=h2)
    assert res.json() == {"ok": True, "room_deleted": False}
    assert all(u2 not in a for a in _acl_users(hs))
    assert (await http.post(f"{BASE}/rooms/{room['id']}/leave", headers=h2)).status_code == 404
    assert (await _join(http, h2, room["code"])).status_code == 200  # leaving isn't a ban
    res = await http.post(f"{BASE}/rooms/{room['id']}/leave", headers=h1)  # the owner
    assert res.json() == {"ok": True, "room_deleted": True}
    assert await _members(container) == []
    assert json.loads(hs.policy) == {"acls": [], "grants": []}
    assert (await http.get(f"{BASE}/rooms", headers=h2)).json() == []


async def test_headscale_down_changes_nothing(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, _ = await _player(container, 26)
    h2, _ = await _player(container, 27)
    room = await _create(http, h1)
    before = (hs.policy, await _members(container))

    hs.fail_put = True  # the PUT is refused → the join is rolled back
    res = await _join(http, h2, room["code"])
    assert (res.status_code, res.json()) == (503, {"detail": "lan_unavailable"})
    assert (hs.policy, await _members(container)) == before

    hs.fail_put, hs.down = False, True  # unreachable
    res = await http.post(f"{BASE}/rooms", headers=h2, json={"name": "x"})
    assert res.status_code == 503
    res = await http.post(f"{BASE}/rooms/{room['id']}/leave", headers=h1)
    assert res.status_code == 503
    assert (await http.post(f"{BASE}/device", headers=h1, json={})).status_code == 503
    async with container.uow() as uow:
        assert await uow.lan_rooms.count() == 1
    assert "lan:policy-lock" not in container.redis.store  # the lock was released


async def test_failed_commit_puts_the_db_policy_back(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PUT landed but the commit failed → the DB policy is re-PUT (best-effort)."""
    http, container = client
    hs = await _lan(container)
    h1, s1 = await _player(container, 28)
    h2, _ = await _player(container, 29)
    room = await _create(http, h1)
    assert (await http.get(f"{BASE}/status", headers=h2)).status_code == 200  # token resolved
    from src.infrastructure.database.uow import UnitOfWork

    real_commit = UnitOfWork.commit
    calls = {"n": 0}

    async def failing_commit(self: UnitOfWork) -> None:
        calls["n"] += 1
        raise RuntimeError("db went away")

    monkeypatch.setattr(UnitOfWork, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        await _join(http, h2, room["code"])
    monkeypatch.setattr(UnitOfWork, "commit", real_commit)
    assert calls["n"] == 1
    assert _acl_users(hs) == [{await _hs_user(container, s1)}]  # the join's PUT was undone


async def test_busy_lock_is_503(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    http, container = client
    await _lan(container)
    h1, _ = await _player(container, 30)
    monkeypatch.setattr(lan_service, "_LOCK_WAIT", 0.0)
    container.redis.store["lan:policy-lock"] = "someone-else"
    res = await http.post(f"{BASE}/rooms", headers=h1, json={"name": "x"})
    assert (res.status_code, res.json()) == (503, {"detail": "lan_busy"})
    assert container.redis.store["lan:policy-lock"] == "someone-else"  # not ours to drop


async def test_join_is_rate_limited(client: Client) -> None:
    http, container = client
    await _lan(container)
    h, sub_id = await _player(container, 31)
    codes = [(await _join(http, h, f"AAAAA{c}")).status_code for c in "BCDEFGHJKMN"]
    assert codes[:10] == [404] * 10 and codes[10] == 429
    # per IP as well: other subscriptions from the same address hit the IP ceiling
    container.redis.store.pop(f"rl:auth:lan-join:{sub_id}")
    container.redis.store["rl:auth:lan-join-ip:127.0.0.1"] = "30"
    assert (await _join(http, h, "AAAAAB")).status_code == 429


async def test_rooms_poll_uses_the_node_cache(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, _ = await _player(container, 32)
    await _create(http, h1)
    calls = {"n": 0}
    real = hs.list_nodes

    async def counting(user: str | None = None) -> Any:
        calls["n"] += 1
        return await real(user)

    hs.list_nodes = counting  # type: ignore[method-assign]
    _fresh(container)
    for _ in range(5):
        assert (await http.get(f"{BASE}/rooms", headers=h1)).status_code == 200
    assert calls["n"] == 1


# --- reset-link ------------------------------------------------------------------------------
async def test_reset_link_expires_lan_devices(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h, _ = await _player(container, 33)  # tg 6600033
    body = (await http.post(f"{BASE}/device", headers=h, json={})).json()
    node = hs.register(body["auth_key"], name=body["hostname"])
    await http.post(f"{BASE}/device", headers=h, json={})  # a pending key too
    res = await http.post("/api/cabinet/subscription/reset-link", headers=_tma_headers(6600033))
    assert res.status_code == 200, res.text
    assert node.id in hs.expired
    assert hs.pending_keys() == 0


# --- periodic cleanup ----------------------------------------------------------------------------
async def test_cleanup_removes_lapsed_and_blocked(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    hs = await _lan(container, LAN_MAX_DEVICES=2)
    h1, s1 = await _player(container, 34)
    h2, s2 = await _player(container, 35)
    h3, s3 = await _player(container, 36)
    h4, s4 = await _player(container, 37)
    room_a = await _create(http, h1, "A")  # s1 owns A; s2 joins
    room_b = await _create(http, h3, "B")  # s3 owns B; s1 and s4 join
    await _join(http, h2, room_a["code"])
    await _join(http, h1, room_b["code"])
    await _join(http, h4, room_b["code"])
    u1, u2, u3, u4 = [await _hs_user(container, s) for s in (s1, s2, s3, s4)]
    n1 = hs.add_node(u1, name="cw-000001")
    n4 = hs.add_node(u4, name="cw-000004")
    stray = hs.add_node("lan-00000dead0")  # an account whose subscription no longer exists
    foreign = hs.add_node("admin")  # not ours — never touched
    tagged = hs.add_node("tagged-devices")
    # s2: three devices with a limit of 2 → the newest goes; one has a PC name → renamed
    old = hs.add_node(u2, name="DESKTOP-PETYA", created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    mid = hs.add_node(u2, name="cw-0000b2", created_at=dt.datetime(2026, 2, 1, tzinfo=dt.UTC))
    new = hs.add_node(u2, name="cw-0000b3", created_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC))

    await _expire(container, s1)  # s1 lapses
    async with container.uow() as uow:  # s4's account is blocked
        sub = await uow.subscriptions.get(s4)
        assert sub is not None
        user = await uow.users.get(sub.user_id)
        assert user is not None
        user.status = UserStatus.BLOCKED
        await uow.commit()

    monkeypatch.setattr(tasks, "get_container", lambda: container)
    stats = await tasks.lan_cleanup()
    assert stats == {
        "removed": 3,  # s1 from A (owner) and B, s4 from B
        "policy_pushed": 1,
        "expired_nodes": 3,
        "deleted": 1,
        "renamed": 1,
    }
    async with container.uow() as uow:
        rooms = await uow.lan_rooms.list()
    assert [r.name for r in rooms] == ["B"]  # s1's own room is gone, B stays without s1/s4
    assert await _members(container) == [(room_b["id"], s3)]
    assert _acl_users(hs) == [{u3}]
    assert sorted(hs.expired) == sorted([n1.id, n4.id, stray.id])
    assert foreign.id not in hs.expired and tagged.id not in hs.expired
    assert new.id not in hs.nodes and mid.id in hs.nodes and old.id in hs.nodes
    assert hs.nodes[old.id].name.startswith("cw-")

    # a second run only re-PUTs the policy (always — it is the source of truth)
    assert await tasks.lan_cleanup() == NO_STATS
    hs.policy = "{}"  # a policy edited behind our back is put back
    await tasks.lan_cleanup()
    assert _acl_users(hs) == [{u3}]


async def test_cleanup_with_headscale_down_keeps_the_rooms(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, s1 = await _player(container, 38)
    await _create(http, h1)
    await _expire(container, s1)
    hs.down = True
    stats = await lan_service.cleanup(container)  # type: ignore[arg-type]
    assert stats["removed"] == 0 and stats["policy_pushed"] == 0
    assert await _members(container) != []  # policy not updated → DB not changed either
    hs.down = False
    await lan_service.cleanup(container)  # type: ignore[arg-type]
    assert await _members(container) == []


async def test_cleanup_skips_when_not_configured(client: Client) -> None:
    _, container = client
    stats = await lan_service.cleanup(container)  # type: ignore[arg-type]
    assert stats == {**NO_STATS, "policy_pushed": 0}


# --- admin -----------------------------------------------------------------------------------
async def test_admin_settings_rooms_and_check(client: Client) -> None:
    http, container = client
    auth = await _login(http)
    res = await http.post("/api/admin/settings/lan/check", headers=auth)
    assert res.json() == {
        "ok": False,
        "error": "Не заданы адрес Headscale (https://) или API-ключ",
    }

    hs = await _lan(container)
    h1, s1 = await _player(container, 39)
    room = await _create(http, h1, "Комната")
    hs.add_node(await _hs_user(container, s1))

    params = (await http.get("/api/admin/settings?lang=ru", headers=auth)).json()
    lan = {p["key"]: p for p in params["params"] if p["category"] == "lan"}
    assert set(lan) == {
        "LAN_ENABLED",
        "LAN_LOGIN_SERVER",
        "LAN_HEADSCALE_API_KEY",
        "LAN_MAX_ROOM_MEMBERS",
        "LAN_MAX_ROOMS_OWNED",
        "LAN_MAX_ROOMS_JOINED",
        "LAN_MAX_DEVICES",
    }
    assert lan["LAN_HEADSCALE_API_KEY"]["type"] == "secret"
    assert "hs-secret-key" not in json.dumps(params)  # write-only
    assert {"id": "lan", "name": "LAN для игр"} in params["categories"]

    rooms = (await http.get("/api/admin/settings/lan/rooms", headers=auth)).json()
    assert rooms["total"] == 1
    (item,) = rooms["items"]
    assert item["name"] == "Комната" and item["members"] == 1
    assert item["owner_subscription_id"] == s1
    assert room["code"] not in json.dumps(rooms)  # a code is a join credential

    hs.policy = ""
    res = await http.post("/api/admin/settings/lan/check", headers=auth)
    assert res.json() == {
        "ok": True,
        "database": True,
        "users": 1,
        "nodes": 1,
        "online": 1,
        "policy": "записана",
    }
    assert _acl_users(hs) == [{await _hs_user(container, s1)}]  # the trial PUT wrote it

    hs.file_mode = True
    res = await http.post("/api/admin/settings/lan/check", headers=auth)
    assert res.json() == {
        "ok": False,
        "error": "policy.mode не database — бот не может записать политику доступа",
    }
    hs.file_mode, hs.fail_put = False, True
    res = await http.post("/api/admin/settings/lan/check", headers=auth)
    assert res.json() == {"ok": False, "error": "Headscale ответил ошибкой 500"}
    hs.fail_put, hs.down = False, True
    res = await http.post("/api/admin/settings/lan/check", headers=auth)
    assert res.json() == {"ok": False, "error": "Headscale недоступен"}


async def test_admin_login_server_https_only_and_clears_the_key(client: Client) -> None:
    http, container = client
    auth = await _login(http)
    await _lan(container)
    for bad in ("http://lan.example.ru", "https://user:pw@lan.example.ru", "https://x.ru/api"):
        res = await http.patch(
            "/api/admin/settings", headers=auth, json={"changes": {"LAN_LOGIN_SERVER": bad}}
        )
        assert res.status_code == 400, bad
    # same server (normalised) keeps the key
    res = await http.patch(
        "/api/admin/settings",
        headers=auth,
        json={"changes": {"LAN_LOGIN_SERVER": "https://lan.cipherway.ru/"}},
    )
    assert res.status_code == 200
    assert (await lan_service.load_config(container)).api_key == "hs-secret-key"  # type: ignore[arg-type]
    # a new server drops the old key (it was issued by the old one)
    container.bot_config.invalidate()
    res = await http.patch(
        "/api/admin/settings",
        headers=auth,
        json={"changes": {"LAN_LOGIN_SERVER": "https://lan2.example.ru"}},
    )
    assert res.status_code == 200 and "LAN_HEADSCALE_API_KEY" in res.json()["applied"]
    cfg = await lan_service.load_config(container)  # type: ignore[arg-type]
    assert (cfg.login_server, cfg.api_key) == ("https://lan2.example.ru", "")
    # ... unless the same PATCH brings a new key
    res = await http.patch(
        "/api/admin/settings",
        headers=auth,
        json={
            "changes": {
                "LAN_LOGIN_SERVER": "https://lan3.example.ru",
                "LAN_HEADSCALE_API_KEY": "new-key",
            }
        },
    )
    cfg = await lan_service.load_config(container)  # type: ignore[arg-type]
    assert (cfg.login_server, cfg.api_key) == ("https://lan3.example.ru", "new-key")


async def test_room_rows_cascade_with_the_subscription_model() -> None:
    """The model wires the FKs with ON DELETE CASCADE (Postgres drops rooms/memberships/bans
    of a deleted subscription; the cleanup then re-applies the policy)."""
    for table, column in (
        (LanRoom.__table__, "owner_subscription_id"),
        (LanRoomMember.__table__, "subscription_id"),
        (LanRoomMember.__table__, "room_id"),
        (LanRoomBan.__table__, "room_id"),
        (LanRoomBan.__table__, "subscription_id"),
    ):
        (fk,) = table.c[column].foreign_keys
        assert fk.ondelete == "CASCADE"


# --- second review: owner-only LAN server/key, reset, join brake, accounts ------------------
async def _plain_admin(http: httpx.AsyncClient, container: ApiTestContainer) -> dict[str, str]:
    from src.application.services.ids import generate_referral_code
    from src.core.enums import AuthType, Role
    from src.core.security import hash_password
    from src.infrastructure.database.models.user import User

    async with container.uow() as uow:
        await uow.users.add(
            User(
                username="lan_plain_admin",
                auth_type=AuthType.EMAIL,
                role=Role.ADMIN,
                referral_code=generate_referral_code(),
                password_hash=hash_password("PlainAdmin123!"),
            )
        )
        await uow.commit()
    res = await http.post(
        "/api/admin/auth/login",
        json={"username": "lan_plain_admin", "password": "PlainAdmin123!"},
    )
    return {"Authorization": f"Bearer {res.json()['token']}"}


async def _audit(container: ApiTestContainer, action: str) -> list[dict[str, Any]]:
    async with container.uow() as uow:
        return [a.payload for a in await uow.audit.list(action=action)]


async def _patch(http: httpx.AsyncClient, auth: dict[str, str], **changes: Any) -> Any:
    return await http.patch("/api/admin/settings", headers=auth, json={"changes": changes})


async def test_only_owner_changes_lan_server_and_key(client: Client) -> None:
    http, container = client
    await _lan(container)
    owner, plain = await _login(http), await _plain_admin(http, container)
    for changes in (
        {"LAN_LOGIN_SERVER": "https://evil.example.ru"},
        {"LAN_HEADSCALE_API_KEY": "stolen"},
    ):
        res = await _patch(http, plain, **changes)
        assert res.status_code == 403, changes
    # other LAN params, an unchanged server and the masked key round-trip are fine
    res = await _patch(
        http,
        plain,
        LAN_MAX_DEVICES=4,
        LAN_LOGIN_SERVER="https://lan.cipherway.ru/",
        LAN_HEADSCALE_API_KEY="••••••••",
    )
    assert res.status_code == 200, res.text
    cfg = await lan_service.load_config(container)  # type: ignore[arg-type]
    assert (cfg.login_server, cfg.api_key, cfg.max_devices) == (
        "https://lan.cipherway.ru",
        "hs-secret-key",
        4,
    )
    res = await http.post(
        "/api/admin/settings/reset", headers=plain, json={"keys": ["LAN_LOGIN_SERVER"]}
    )
    assert res.status_code == 403
    # the owner may; the old and new server land in the audit log
    res = await _patch(http, owner, LAN_LOGIN_SERVER="https://lan2.example.ru")
    assert res.status_code == 200
    (entry,) = [p for p in await _audit(container, "settings.patch") if "lan_server_old" in p]
    assert (entry["lan_server_old"], entry["lan_server_new"]) == (
        "https://lan.cipherway.ru",
        "https://lan2.example.ru",
    )


async def test_resetting_the_server_drops_the_key(client: Client) -> None:
    http, container = client
    await _lan(container)
    owner = await _login(http)
    reset = "/api/admin/settings/reset"
    # the server is still the default → resetting it keeps the key
    res = await http.post(reset, headers=owner, json={"keys": ["LAN_LOGIN_SERVER"]})
    assert res.json() == {"ok": True, "reset": ["LAN_LOGIN_SERVER"]}
    assert (await lan_service.load_config(container)).api_key == "hs-secret-key"  # type: ignore[arg-type]
    # a custom server + its key → resetting the server drops that key too
    await _patch(
        http, owner, LAN_LOGIN_SERVER="https://lan2.example.ru", LAN_HEADSCALE_API_KEY="k2"
    )
    res = await http.post(reset, headers=owner, json={"keys": ["LAN_LOGIN_SERVER"]})
    assert res.json() == {"ok": True, "reset": ["LAN_LOGIN_SERVER", "LAN_HEADSCALE_API_KEY"]}
    cfg = await lan_service.load_config(container)  # type: ignore[arg-type]
    assert (cfg.login_server, cfg.api_key) == ("https://lan.cipherway.ru", "")
    entry = (await _audit(container, "settings.reset"))[-1]
    assert entry["lan_server_old"] == "https://lan2.example.ru"


class _Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    async def notify_admins(self, text: str, *, topic: str | None = None) -> None:
        self.sent.append((text, topic))


async def test_global_join_brake(client: Client) -> None:
    http, container = client
    await _lan(container)
    alerts = _Alerts()
    container.notifier = alerts  # type: ignore[assignment]
    h1, _ = await _player(container, 40)
    h2, _ = await _player(container, 41)
    h3, _ = await _player(container, 42)
    room = await _create(http, h1)
    container.redis.store["lan:join-fail"] = "198"  # failures from many other subscriptions
    assert (await _join(http, h2, "ZZZZZZ")).status_code == 404  # 199
    assert alerts.sent == []
    assert (await _join(http, h2, "bad")).status_code == 404  # 200 → brake + one alert
    assert len(alerts.sent) == 1 and alerts.sent[0][1] == "alerts"
    assert "неверным кодом" in alerts.sent[0][0]
    # now even a right code from someone else waits until the window ends
    res = await _join(http, h3, room["code"])
    assert (res.status_code, res.json()) == (503, {"detail": "lan_busy"})
    assert len(alerts.sent) == 1  # one alert per hour
    container.redis.store.pop("lan:join-fail")
    assert (await _join(http, h3, room["code"])).status_code == 200


async def test_rooms_view_skips_unknown_accounts_and_foreign_users(client: Client) -> None:
    http, container = client
    hs = await _lan(container)
    h1, s1 = await _player(container, 43)
    h2, s2 = await _player(container, 44)
    h3, s3 = await _player(container, 45)
    room = await _create(http, h1)
    await _join(http, h2, room["code"])
    await _join(http, h3, room["code"])
    from src.infrastructure.database.models.lan_room import LanAccount

    async with container.uow() as uow:
        await uow.lan_accounts.delete_by(subscription_id=s2)  # a member without an account
        acc = await uow.lan_accounts.find_one(subscription_id=s3)
        assert isinstance(acc, LanAccount)
        acc.hs_user = "admin"  # not one of our names → its nodes are never indexed
        await uow.commit()
    hs.add_node("admin", name="cw-00ad00")
    hs.add_node(await _hs_user(container, s1), name="cw-00aa01")
    _fresh(container)
    members = (await http.get(f"{BASE}/rooms", headers=h1)).json()[0]["members"]
    assert [m["label"] for m in members] == ["Игрок 1", "Игрок 2"]  # s2 skipped
    assert members[0]["devices"] == [
        {"name": "cw-00aa01", "ip": members[0]["devices"][0]["ip"], "online": True}
    ]
    assert members[1]["devices"] == []  # the «admin» user's node isn't shown


async def test_lan_check_is_audited(client: Client) -> None:
    http, container = client
    await _lan(container)
    auth = await _login(http)
    assert (await http.post("/api/admin/settings/lan/check", headers=auth)).json()["ok"] is True
    assert await _audit(container, "lan.check") == [{"ok": True, "error": None}]
