"""list_users(): every panel user, across 3.x keyset pages (/api/users/stream)."""

from __future__ import annotations

import httpx
import respx

from src.infrastructure.remnawave.client import RemnawaveHttpClient


def _row(i: int, **extra: object) -> dict:
    return {
        "id": i,
        "uuid": f"00000000-0000-0000-0000-{i:012d}",
        "shortUuid": f"short{i}",
        "username": f"user_{i}",
        "status": "ACTIVE",
        "expireAt": "2030-01-01T00:00:00.000Z",
        "trafficLimitBytes": 0,
        "userTraffic": {"usedTrafficBytes": 1000 * i},
        **extra,
    }


@respx.mock
async def test_v3_stream_follows_next_cursor_until_has_more_is_false() -> None:
    route = respx.get("https://panel.test/api/users/stream").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "response": {"users": [_row(1), _row(2)], "nextCursor": "2", "hasMore": True}
                },
            ),
            httpx.Response(
                200,
                json={
                    "response": {
                        "users": [
                            _row(3, description="tg:5072755730", createdAt="2026-09-01T10:00:00Z")
                        ],
                        "nextCursor": None,
                        "hasMore": False,
                    }
                },
            ),
        ]
    )
    client = RemnawaveHttpClient(httpx.AsyncClient(base_url="https://panel.test"))
    client._mode = 3  # skip the version probe
    users = await client.list_users()
    assert [u.panel_id for u in users] == [1, 2, 3]
    assert route.calls[1].request.url.params["cursor"] == "2"
    assert users[2].description == "tg:5072755730" and users[2].status == "ACTIVE"
    assert users[2].created_at is not None


@respx.mock
async def test_an_incomplete_list_is_an_error_not_the_whole_panel() -> None:
    """A partial answer must never pass for «everyone on the panel»: the orphan clean-up and
    the panel sync decide «not on the panel» from it."""
    import pytest

    from src.core.exceptions import RemnawaveTransientError

    respx.get("https://panel.test/api/users/stream").mock(
        return_value=httpx.Response(
            200, json={"response": {"users": [_row(1)], "total": 3, "hasMore": False}}
        )
    )
    client = RemnawaveHttpClient(httpx.AsyncClient(base_url="https://panel.test"))
    client._mode = 3
    with pytest.raises(RemnawaveTransientError):
        await client.list_users()


@respx.mock
async def test_more_promised_but_no_cursor_is_incomplete() -> None:
    import pytest

    from src.core.exceptions import RemnawaveTransientError

    respx.get("https://panel.test/api/users/stream").mock(
        return_value=httpx.Response(
            200, json={"response": {"users": [_row(1)], "hasMore": True, "nextCursor": None}}
        )
    )
    client = RemnawaveHttpClient(httpx.AsyncClient(base_url="https://panel.test"))
    client._mode = 3
    with pytest.raises(RemnawaveTransientError):
        await client.list_users()


@respx.mock
async def test_get_user_strict_tells_not_found_from_a_dead_panel() -> None:
    import pytest

    from src.application.dto.panel import PanelUserRef
    from src.core.exceptions import RemnawaveAuthError

    respx.get("https://panel.test/api/users/5").mock(return_value=httpx.Response(404, json={}))
    respx.get("https://panel.test/api/users/6").mock(return_value=httpx.Response(401, json={}))
    client = RemnawaveHttpClient(httpx.AsyncClient(base_url="https://panel.test"))
    client._mode = 3
    assert await client.get_user_strict(PanelUserRef(panel_id=5)) is None
    with pytest.raises(RemnawaveAuthError):
        await client.get_user_strict(PanelUserRef(panel_id=6))
    assert await client.get_user(PanelUserRef(panel_id=6)) is None  # the lenient one as before


@respx.mock
async def test_connection_facts_from_user_traffic() -> None:
    """«Клиенты» reads online/first-connection/last node from the same listing."""
    respx.get("https://panel.test/api/users/stream").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "users": [
                        _row(
                            1,
                            userTraffic={
                                "usedTrafficBytes": "5",
                                "onlineAt": "2026-11-23T10:00:00Z",
                                "firstConnectedAt": "2026-11-01T10:00:00Z",
                                "lastConnectedNodeUuid": "6f1c2a1e-0000-4000-8000-000000000001",
                            },
                        ),
                        # older shape: node named inline, never connected
                        _row(2, lastConnectedNode={"nodeName": "Франкфурт", "countryCode": "DE"}),
                    ],
                    "hasMore": False,
                }
            },
        )
    )
    client = RemnawaveHttpClient(httpx.AsyncClient(base_url="https://panel.test"))
    client._mode = 3
    first, second = await client.list_users()
    assert first.online_at is not None and first.online_at.hour == 10
    assert first.first_connected_at is not None
    assert first.last_node_uuid == "6f1c2a1e-0000-4000-8000-000000000001"
    assert first.traffic_used_bytes == 5
    assert second.online_at is None and second.first_connected_at is None
    assert (second.last_node_name, second.last_node_country) == ("Франкфурт", "DE")


@respx.mock
async def test_all_hwid_devices_in_pages_with_their_owner() -> None:
    route = respx.get("https://panel.test/api/hwid/devices").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "response": {
                        "total": 3,
                        "devices": [
                            {"hwid": "a", "userId": 7, "platform": "iOS"},
                            {"hwid": "b", "userUuid": "00000000-0000-0000-0000-000000000009"},
                        ],
                    }
                },
            ),
            httpx.Response(
                200,
                json={"response": {"total": 3, "devices": [{"hwid": "c", "user": {"id": "7"}}]}},
            ),
        ]
    )
    client = RemnawaveHttpClient(httpx.AsyncClient(base_url="https://panel.test"))
    client._mode = 3
    devices = await client.list_hwid_devices()
    assert [(d.hwid, d.user_key) for d in devices] == [
        ("a", "id:7"),
        ("b", "uuid:00000000-0000-0000-0000-000000000009"),
        ("c", "id:7"),
    ]
    assert route.calls[1].request.url.params["start"] == "2"
