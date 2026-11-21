"""HeadscaleClient against a mocked HTTP transport: paths, Bearer auth, request bodies and
response parsing as in headscale v0.29.7 (grpc-gateway JSON), plus error mapping."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable

import httpx
import pytest

from src.core.exceptions import (
    HeadscaleAuthError,
    HeadscaleError,
    HeadscaleNotFound,
    HeadscalePolicyReadOnly,
)
from src.infrastructure.headscale.client import HeadscaleClient

SERVER = "https://lan.example.test"

_NODE = {
    "id": "7",
    "machineKey": "mkey:secret",
    "nodeKey": "nodekey:secret",
    "ipAddresses": ["fd7a:115c:a1e0::7", "100.64.0.7"],
    "name": "DESKTOP-1",
    "givenName": "desktop-1",
    "user": {"id": "3", "name": "sub-42"},
    "lastSeen": "2026-11-18T10:00:00.123456789Z",
    "expiry": "0001-01-01T00:00:00Z",
    "online": True,
}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> HeadscaleClient:
    return HeadscaleClient(SERVER + "/", "hs-api-key", transport=httpx.MockTransport(handler))


async def test_requests_and_parsing() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path == "/api/v1/user" and req.method == "GET":
            assert req.url.params.get("name") == "sub-42"
            return httpx.Response(200, json={"users": []})
        if path == "/api/v1/user" and req.method == "POST":
            return httpx.Response(200, json={"user": {"id": "3", "name": "sub-42"}})
        if path == "/api/v1/preauthkey":
            return httpx.Response(
                200,
                json={"preAuthKey": {"key": "pak-1", "expiration": "2026-11-18T10:10:00Z"}},
            )
        if path == "/api/v1/node" and req.method == "GET":
            return httpx.Response(200, json={"nodes": [_NODE]})
        if path == "/api/v1/node/7/expire":
            return httpx.Response(200, json={"node": _NODE})
        if path == "/api/v1/node/7" and req.method == "DELETE":
            return httpx.Response(200, json={})
        if path == "/api/v1/policy" and req.method == "PUT":
            return httpx.Response(200, json={"policy": "{}", "updatedAt": None})
        if path == "/api/v1/policy":
            return httpx.Response(200, json={"policy": '{"acls": []}'})
        if path == "/api/v1/health":
            return httpx.Response(200, json={"databaseConnectivity": True})
        return httpx.Response(500)

    expires = dt.datetime(2026, 11, 18, 10, 10, tzinfo=dt.UTC)
    async with _client(handler) as hs:
        assert await hs.health() is True
        user = await hs.ensure_user("sub-42")  # not found → created
        key = await hs.create_preauth_key(user.id, expires_at=expires)
        nodes = await hs.list_nodes("sub-42")
        await hs.expire_node(7)
        await hs.delete_node(7)
        assert await hs.get_policy() == '{"acls": []}'
        await hs.set_policy('{"acls": [], "grants": []}')

    assert all(r.headers["authorization"] == "Bearer hs-api-key" for r in seen)
    assert (user.id, user.name) == (3, "sub-42")
    assert key.key == "pak-1" and key.expiration == expires
    by_path = {(r.method, r.url.path): r for r in seen}
    assert json.loads(by_path[("POST", "/api/v1/user")].content) == {"name": "sub-42"}
    assert json.loads(by_path[("POST", "/api/v1/preauthkey")].content) == {
        "user": "3",
        "reusable": False,
        "ephemeral": False,
        "expiration": "2026-11-18T10:10:00Z",
        "aclTags": [],
    }
    assert by_path[("GET", "/api/v1/node")].url.params.get("user") == "sub-42"
    assert by_path[("POST", "/api/v1/node/7/expire")].content == b""  # expiry = now
    assert json.loads(by_path[("PUT", "/api/v1/policy")].content) == {
        "policy": '{"acls": [], "grants": []}'
    }
    (node,) = nodes
    assert (node.id, node.name, node.user, node.ipv4, node.online) == (
        7,
        "desktop-1",
        "sub-42",
        "100.64.0.7",
        True,
    )
    assert node.user_id == 3
    assert node.expiry is None and not node.is_expired()  # Go zero time = never expires
    assert node.last_seen == dt.datetime(2026, 11, 18, 10, 0, 0, 123456, tzinfo=dt.UTC)


async def test_list_users_keeps_exact_name_only() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"users": [{"id": "1", "name": "sub-420"}]})

    async with _client(handler) as hs:
        assert await hs.list_users("sub-42") == []


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, HeadscaleAuthError),
        (403, HeadscaleAuthError),
        (404, HeadscaleNotFound),
        (500, HeadscaleError),
    ],
)
async def test_errors_are_mapped(status: int, exc: type[Exception]) -> None:
    async with _client(lambda req: httpx.Response(status, text="secret-body-boom")) as hs:
        with pytest.raises(exc) as info:
            await hs.delete_node(1)
    assert "secret-body-boom" not in str(info.value)  # bodies go to logs, not to the admin
    assert getattr(info.value, "status", None) == status


async def test_file_mode_policy_put_is_read_only() -> None:
    body = {"code": 2, "message": "update is disabled for modes other than 'database'"}
    async with _client(lambda req: httpx.Response(500, json=body)) as hs:
        with pytest.raises(HeadscalePolicyReadOnly):
            await hs.set_policy("{}")


async def test_preauth_keys_rename() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "GET":
            keys = [
                {
                    "id": "1",
                    "user": {"id": "5", "name": "lan-aaaa000001"},
                    "used": False,
                    "expiration": "2999-01-01T00:00:00Z",
                    "key": "hskey-auth-x-***",
                },
                {"id": "2", "user": {"id": "6", "name": "lan-aaaa000001"}, "used": False},
                {"id": "3", "user": {"id": "5", "name": "lan-aaaa000001"}, "used": True},
            ]
            return httpx.Response(200, json={"preAuthKeys": keys})
        return httpx.Response(200, json={})

    async with _client(handler) as hs:
        keys = await hs.list_preauth_keys(5)
        await hs.expire_preauth_key(1)
        await hs.rename_node(7, "cw-abc123")
    assert [(k.id, k.used, k.is_pending()) for k in keys] == [(1, False, True), (3, True, False)]
    assert (seen[1].method, seen[1].url.path) == ("POST", "/api/v1/preauthkey/expire")
    assert json.loads(seen[1].content) == {"id": "1"}
    assert (seen[2].method, seen[2].url.path) == ("POST", "/api/v1/node/7/rename/cw-abc123")


async def test_unreachable_server_is_headscale_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    async with _client(handler) as hs:
        with pytest.raises(HeadscaleError, match="unreachable"):
            await hs.list_nodes()


def test_not_configured_is_refused() -> None:
    with pytest.raises(HeadscaleError):
        HeadscaleClient("", "key")
    with pytest.raises(HeadscaleError):
        HeadscaleClient(SERVER, "")
    with pytest.raises(HeadscaleError):
        HeadscaleClient("ftp://lan.example.test", "key")
    with pytest.raises(HeadscaleError):  # the API key would travel in the clear
        HeadscaleClient("http://lan.example.test", "key")


async def test_every_call_has_an_overall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from src.infrastructure.headscale import client as client_mod

    monkeypatch.setattr(client_mod, "CALL_DEADLINE", 0.05)

    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    async with _client(slow) as hs:  # type: ignore[arg-type]
        with pytest.raises(HeadscaleError, match="deadline"):
            await hs.get_policy()
