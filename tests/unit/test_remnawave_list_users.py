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
