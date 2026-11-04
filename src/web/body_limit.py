"""Request-body size caps for public, unauthenticated-at-parse-time endpoints.

FastAPI reads (and multipart-parses) a request body BEFORE it runs the route's auth
dependency, so without a cap anyone could make the server swallow huge bodies on the
Windows-app API or the support-photo upload. This pure-ASGI middleware refuses an oversized
``Content-Length`` up front and, for chunked bodies, stops feeding the app once the cap is
crossed and answers 413 in place of whatever the app would have said about the cut body.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send

KB = 1024
MB = 1024 * KB

# (path, is_prefix, max bytes) — first match wins, so exact photo routes precede the prefix.
DEFAULT_RULES: tuple[tuple[str, bool, int], ...] = (
    ("/api/app/v1/support/photo", False, 11 * MB),
    ("/api/cabinet/support/photo", False, 11 * MB),
    ("/api/app/v1/", True, 16 * KB),
)


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, rules: Sequence[tuple[str, bool, int]] = DEFAULT_RULES):
        self.app = app
        self.rules = tuple(rules)

    def _limit(self, path: str) -> int | None:
        for rule, is_prefix, limit in self.rules:
            if path.startswith(rule) if is_prefix else path == rule:
                return limit
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope.get("path", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > limit:
                    await _reject(send)
                    return
                break

        received = 0
        exceeded = False
        replied = False

        async def capped_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal replied
            if not exceeded:
                await send(message)
                return
            # The app answered a body we cut short — say what actually happened, once.
            if not replied and message["type"] == "http.response.start":
                replied = True
                await _reject(send)

        await self.app(scope, capped_receive, guarded_send)


async def _reject(send: Send) -> None:
    body = json.dumps({"detail": "request body too large"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
