"""Read-only admin API keys (``cwk_...``) for AI agents: helpers, guards and response filter.

The key itself is checked in ``deps.require_admin`` (``Authorization: Bearer cwk_...``); this
module holds what that check and the app need around it:

* key generation / hashing (only the SHA-256 hash is stored);
* the screens and paths a key may never read, whatever its ``allowed_screens`` say;
* :class:`AdminKeyResponseFilter` — ASGI middleware that, for a request authenticated by a key,
  masks e-mails / phones / credentials in the JSON body (one place for every endpoint, not a
  per-route change) and appends the request to the key's journal in Redis.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any, cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.core.logging import get_logger

log = get_logger(__name__)

API_KEY_PREFIX = "cwk_"
KEY_RATE_LIMIT = 120  # requests per minute, per key
KEY_LOG_SIZE = 1000  # journal entries kept per key (newest first)
# Set on request.state by require_admin once a key is recognised (its id).
KEY_STATE_ATTR = "admin_api_key_id"

# Closed to keys regardless of a key's allowed_screens. Built from a pass over every GET under
# /api/admin: screens that hand out secrets/credentials or whole-file exports, plus the
# owner-only account screens. (Secret settings in /settings are already masked by the registry,
# payment credentials in /providers are never echoed — /providers and /site are closed anyway:
# they exist only to manage those secrets, so a future field leak there can't reach a key.)
KEY_BLOCKED_SCREENS = frozenset(
    {
        "admins",  # staff accounts (owner-only)
        "admin-keys",  # these very keys and their journals (owner-only)
        "migration",  # foreign bot DB dumps / Postgres DSNs
        "upload",  # media upload
        "providers",  # payment gateway credentials
        "site",  # SMTP password, OAuth client secrets
        "maintenance",  # backups, restarts
    }
)
# Single read endpoints closed to keys inside otherwise readable screens.
KEY_BLOCKED_PATHS = frozenset(
    {
        "/api/admin/bot-menu/export",  # full export file of the bot menu + its config
        "/api/admin/users/panel-sync",  # pulls every panel user at once (heavy)
    }
)
# Credentials embedded in otherwise useful answers: replaced with "***" for keys.
# install_code / claim_url — a live router install code and router claim link (router card);
# subscription_url — the customer's VPN subscription link (user card); short_id — for imported
# subscriptions it IS the panel's shortUuid, i.e. the subscription link; panel_url — the
# Remnawave panel address (servers screen).
KEY_REDACTED_FIELDS = frozenset(
    {"install_code", "claim_url", "subscription_url", "short_id", "panel_url"}
)


def _keep_head(value: str) -> str:
    return f"{value[:2]}****"


def _keep_tail(value: str) -> str:
    return f"****{value[-4:]}" if len(value) > 8 else "****"


# Field names that are secret on one endpoint only (``code`` is harmless in report-topics or
# partners, ``details`` elsewhere): masked for keys on exactly these paths.
KEY_PATH_FIELD_MASKS: dict[str, dict[str, Callable[[str], str]]] = {
    # gift / promo codes are money — the key sees activations, not the codes
    "/api/admin/promocodes": {"code": _keep_head},
    # payout requisites (card / phone / wallet)
    "/api/admin/withdrawals": {"details": _keep_tail},
}
# Settings whose plain STR values may carry tracker tokens: hidden in /settings for keys.
KEY_HIDDEN_SETTING_PREFIXES = ("POSTBACK_URL_",)


def generate_api_key() -> str:
    """``cwk_`` + 40 url-safe random chars (shown once, never stored)."""
    return API_KEY_PREFIX + secrets.token_urlsafe(30)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def screen_of(path: str) -> str:
    """First path segment after /api/admin/, e.g. '/api/admin/routers/5/rotate' -> 'routers'."""
    rest = path.removeprefix("/api/admin/").removeprefix("/api/admin")
    return rest.split("/", 1)[0]


def key_path_blocked(path: str) -> bool:
    return screen_of(path) in KEY_BLOCKED_SCREENS or path.rstrip("/") in KEY_BLOCKED_PATHS


async def key_rate_limited(redis: Any, key_id: int) -> bool:
    """Fixed one-minute window per key. INCR and EXPIRE NX go in one MULTI, so the counter can
    never be left without a TTL (which would lock the key out for good). Fail-open like the auth
    limiters: a Redis hiccup must not cut the agent off (the key is read-only either way)."""
    bucket = f"rl:admin_key:{key_id}"
    try:
        pipe = redis.pipeline(transaction=True)
        pipe.incr(bucket)
        pipe.expire(bucket, 60, nx=True)
        n, _ = await pipe.execute()
    except Exception:
        return False
    return int(n) > KEY_RATE_LIMIT


def key_log_name(key_id: int) -> str:
    return f"admin:keylog:{key_id}"


async def read_key_log(redis: Any, key_id: int) -> list[dict[str, Any]]:
    raw = await cast(
        "Awaitable[list[str]]", redis.lrange(key_log_name(key_id), 0, KEY_LOG_SIZE - 1)
    )
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except (TypeError, ValueError):
            continue
    return out


# --- masking -----------------------------------------------------------------------------

_EMAIL_FULL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Zone: letters only (latin or cyrillic) — keeps vless-style uuid@1.2.3.4 out.
_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}")
# Free text: "+"-prefixed numbers and Russian mobiles written from 8 (8 9xx ...) count as
# phones (not inside a longer digit run); other bare digit runs are ids, sums, dates.
_PHONE_IN_TEXT = re.compile(
    r"\+\d[\d\s()\-]{8,}\d"
    r"|(?<!\d)8[\s(-]*9\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"
)


def mask_email(value: str) -> str:
    """'alice@ya.ru' -> 'a***@ya.ru'."""
    local, _, domain = value.rpartition("@")
    return f"{local[:1]}***@{domain}"


def mask_phone(value: str) -> str:
    """'+79991234512' -> '+7***12'."""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 5:
        return "***"
    head = ("+" if value.strip().startswith("+") else "") + digits[0]
    return f"{head}***{digits[-2:]}"


def _mask_text_phone(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return mask_phone(m.group(0)) if 10 <= len(digits) <= 15 else m.group(0)


def _personal_field(name: str) -> bool:
    n = name.lower()
    return "email" in n or "phone" in n or n in ("tel", "mobile")


def _mask_str(value: str, personal: bool) -> str:
    if personal:
        if _EMAIL_FULL.fullmatch(value.strip()):
            return mask_email(value.strip())
        if "@" not in value and len(re.sub(r"\D", "", value)) >= 7:
            return mask_phone(value)
    value = _EMAIL_IN_TEXT.sub(lambda m: mask_email(m.group(0)), value)
    return _PHONE_IN_TEXT.sub(_mask_text_phone, value)


def mask_personal(
    value: Any,
    personal: bool = False,
    field_masks: dict[str, Callable[[str], str]] | None = None,
) -> Any:
    """Recursively mask e-mails and phones in a decoded JSON value (for API-key responses).

    Values of fields named like ``email`` / ``phone`` / ``phone_number`` are masked whole;
    e-mails (and phones) inside any other string are masked in place. Telegram usernames and
    ids are left as they are. Fields in ``KEY_REDACTED_FIELDS`` become "***"; ``field_masks``
    (the per-path rules from ``KEY_PATH_FIELD_MASKS``) mask the named fields their own way.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, str) and v and field_masks and k in field_masks:
                out[k] = field_masks[k](v)
            elif k in KEY_REDACTED_FIELDS and isinstance(v, str) and v:
                out[k] = "***"
            else:
                out[k] = mask_personal(v, _personal_field(str(k)), field_masks)
        return out
    if isinstance(value, list):
        return [mask_personal(v, personal, field_masks) for v in value]
    if isinstance(value, str):
        return _mask_str(value, personal)
    return value


def _hide_settings(data: Any) -> Any:
    """/settings for keys: drop the values of ``KEY_HIDDEN_SETTING_PREFIXES`` params."""
    if isinstance(data, dict) and isinstance(data.get("params"), list):
        for row in data["params"]:
            if (
                isinstance(row, dict)
                and str(row.get("key", "")).startswith(KEY_HIDDEN_SETTING_PREFIXES)
                and row.get("value")
            ):
                row["value"] = "***"
    return data


def mask_for_key(path: str, data: Any) -> Any:
    """Everything an API key's JSON answer goes through (see ``AdminKeyResponseFilter``)."""
    path = path.rstrip("/")
    if path == "/api/admin/settings":
        data = _hide_settings(data)
    return mask_personal(data, field_masks=KEY_PATH_FIELD_MASKS.get(path))


UNSAFE_BODY = json.dumps(
    {"detail": "ответ нельзя безопасно отдать по ключу"}, ensure_ascii=False
).encode()


def _filter_body(start: Message, body: bytes, path: str) -> bytes | None:
    """Masked body for a key, ``body`` itself when empty, or None (fail-closed) when the answer
    can't be checked: not JSON, compressed, or not parseable."""
    if not body:
        return body
    content_type = b""
    for k, v in start.get("headers", []):
        name = k.lower()
        if name == b"content-encoding":
            return None
        if name == b"content-type":
            content_type = v.lower()
    if not content_type.startswith(b"application/json"):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    masked = mask_for_key(path, data)
    return json.dumps(masked, ensure_ascii=False, separators=(",", ":")).encode()


async def _journal(scope: Scope, key_id: int, status: int) -> None:
    app = scope.get("app")
    container = getattr(getattr(app, "state", None), "container", None)
    if container is None:
        return
    query = urllib.parse.unquote(scope.get("query_string", b"").decode("latin-1"))
    path = scope["path"] + (f"?{query}" if query else "")
    entry = json.dumps(
        {
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "method": scope["method"],
            "path": path[:500],
            "status": status,
        },
        ensure_ascii=False,
    )
    name = key_log_name(key_id)
    try:
        await cast("Awaitable[int]", container.redis.lpush(name, entry))
        await cast("Awaitable[bool]", container.redis.ltrim(name, 0, KEY_LOG_SIZE - 1))
    except Exception:
        log.warning("admin key journal not written", key_id=key_id)


class AdminKeyResponseFilter:
    """ASGI middleware for /api/admin/*: once ``require_admin`` has recognised an API key (it
    sets ``request.state.admin_api_key_id``), the JSON response is masked (``mask_personal``)
    and the request lands in the key's journal. Staff sessions pass through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/admin/"):
            await self.app(scope, receive, send)
            return
        # The same dict request.state writes to further down the stack.
        state: dict[str, Any] = scope.setdefault("state", {})
        start: Message | None = None
        chunks: list[bytes] = []
        status = 500

        async def send_filtered(message: Message) -> None:
            nonlocal start, status
            if state.get(KEY_STATE_ATTR) is None:
                await send(message)
                return
            if message["type"] == "http.response.start":
                status = int(message["status"])
                start = message
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            raw = b"".join(chunks)
            body = _filter_body(start, raw, scope["path"])
            if body is None:  # fail-closed: never hand an unchecked body to a key
                status = 502
                body = UNSAFE_BODY
                start = {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            if body is not raw:
                headers = [
                    (k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"
                ]
                headers.append((b"content-length", str(len(body)).encode()))
                start = {**start, "headers": headers}
            await send(start)
            await send({"type": "http.response.body", "body": body})

        try:
            await self.app(scope, receive, send_filtered)
        finally:
            key_id = state.get(KEY_STATE_ATTR)
            if key_id is not None:
                await _journal(scope, int(key_id), status)
