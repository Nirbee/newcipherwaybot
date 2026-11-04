"""Guards in front of the public Windows-app / support endpoints: client-IP trust and the
request-body cap (both run before any route auth)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from starlette.requests import Request

from src.core.config import get_settings
from src.web.body_limit import BodyLimitMiddleware
from src.web.routes.cabinet_auth import _client_ip


def _req(peer: str, **headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 40000),
        }
    )


@pytest.fixture
def trusted(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("WEB__TRUSTED_PROXIES", raising=False)  # the default: loopback + docker
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_proxy_headers_are_trusted_only_from_a_proxy_peer(trusted: None) -> None:
    # straight from the internet: a forged X-Real-IP / XFF is ignored (203.0.113.0/24 is a
    # documentation range that ipaddress calls "private" — it must NOT count as our proxy)
    assert _client_ip(_req("203.0.113.5", x_real_ip="1.2.3.4")) == "203.0.113.5"
    assert _client_ip(_req("8.8.8.8", x_forwarded_for="1.2.3.4")) == "8.8.8.8"
    # from our Caddy on the docker network: the RIGHTMOST XFF hop (what the proxy saw) wins,
    # a forged value on the left and a client-sent X-Real-IP are ignored
    h = {"x_forwarded_for": "6.6.6.6, 198.51.100.8", "x_real_ip": "6.6.6.6"}
    assert _client_ip(_req("172.18.0.3", **h)) == "198.51.100.8"
    # X-Real-IP is only the fallback when the proxy sent no XFF
    assert _client_ip(_req("127.0.0.1", x_real_ip="198.51.100.7")) == "198.51.100.7"
    assert _client_ip(_req("10.0.0.2")) == "10.0.0.2"
    assert _client_ip(_req("::ffff:172.18.0.3", x_forwarded_for="198.51.100.9")) == "198.51.100.9"


def test_trusted_proxies_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB__TRUSTED_PROXIES", "192.0.2.10/32")
    get_settings.cache_clear()
    try:
        assert _client_ip(_req("192.0.2.10", x_forwarded_for="198.51.100.1")) == "198.51.100.1"
        # the docker range is no longer trusted once the owner narrowed the list
        assert _client_ip(_req("172.18.0.3", x_forwarded_for="198.51.100.1")) == "172.18.0.3"
    finally:
        get_settings.cache_clear()


async def _run(app: Any, path: str, headers: list[tuple[bytes, bytes]], chunks: list[bytes]):  # type: ignore[no-untyped-def]
    sent: list[dict[str, Any]] = []
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        body = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
    await BodyLimitMiddleware(app)(scope, receive, send)
    return sent, reads


async def _reading_app(scope: Any, receive: Any, send: Any) -> None:
    more = True
    while more:
        message = await receive()
        more = message.get("more_body", False)
    await send({"type": "http.response.start", "status": 400, "headers": []})
    await send({"type": "http.response.body", "body": b"cut body"})


async def test_oversized_content_length_is_refused_without_reading_the_body() -> None:
    sent, reads = await _run(
        _reading_app, "/api/app/v1/support", [(b"content-length", b"999999999")], [b"x"]
    )
    assert sent[0]["status"] == 413 and reads == 0


async def test_chunked_body_is_cut_at_the_cap_and_answered_413() -> None:
    sent, _ = await _run(_reading_app, "/api/app/v1/support", [], [b"a" * 10000] * 3)
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert b"cut body" not in b"".join(m.get("body", b"") for m in sent)


async def test_photo_routes_get_the_bigger_cap_and_other_paths_none() -> None:
    small = [(b"content-length", b"20000")]
    sent, _ = await _run(_reading_app, "/api/app/v1/support/photo", small, [b"a" * 20000])
    assert sent[0]["status"] == 400  # under 11 MB — the app's own answer passes through
    sent, _ = await _run(
        _reading_app, "/api/cabinet/support/photo", [(b"content-length", b"12000000")], [b"a"]
    )
    assert sent[0]["status"] == 413
    sent, _ = await _run(
        _reading_app, "/api/admin/upload", [(b"content-length", b"50000000")], [b"a"]
    )
    assert sent[0]["status"] == 400  # admin uploads are not capped here
