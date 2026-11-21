"""Headscale REST client (``/api/v1``, ``Authorization: Bearer <api key>``) for LAN Play.

Shapes follow headscale v0.29.7 (``gen/openapiv2/headscale/v1/headscale.swagger.json`` and
``hscontrol/grpcv1.go``): grpc-gateway JSON, lowerCamelCase fields, uint64 ids as strings,
RFC 3339 timestamps (unset ones come as ``null``). Only the calls LAN Play needs:

* users — list (optionally by exact ``name``), create;
* pre-auth keys — create for a user id (single-use, not ephemeral, tag-less, short expiry);
  list (the server lists ALL keys — filtered here by user), expire by id;
* nodes — list (all, or by user NAME — an unknown name is a server error there, so callers
  resolve the user first), delete, expire, rename;
* policy — GET / PUT the whole policy document (needs ``policy.mode: database``; in file mode
  PUT fails with ``ErrPolicyUpdateIsDisabled`` → :class:`HeadscalePolicyReadOnly`).

No retries: every call sits on a user request (or a periodic task that re-runs anyway), so a
dead server fails fast as :class:`HeadscaleError` and the caller answers 503. The API key is
never logged; response bodies go to the logs only (truncated), never into exception texts —
those reach the admin screen.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from src.core.exceptions import (
    HeadscaleAuthError,
    HeadscaleError,
    HeadscaleNotFound,
    HeadscalePolicyReadOnly,
)
from src.core.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 10.0  # httpx per-phase timeout (connect / read / ...)
CALL_DEADLINE = 15.0  # overall ceiling of one call, whatever the phases add up to
# hscontrol/types/policy.go: ErrPolicyUpdateIsDisabled
_POLICY_READ_ONLY = "update is disabled for modes other than 'database'"


def _ts(raw: Any) -> dt.datetime | None:
    """protojson timestamp → aware UTC datetime; null / Go zero time → None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        value = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.year <= 1:  # 0001-01-01T00:00:00Z — Go's zero time.Time
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _id(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _utc_z(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class HsUser:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class HsNode:
    id: int
    name: str
    user: str  # the owning Headscale user's name (``lan-<random>``, see LanAccount)
    ips: tuple[str, ...]
    online: bool
    last_seen: dt.datetime | None
    expiry: dt.datetime | None  # None = never expires
    created_at: dt.datetime | None = None
    user_id: int = 0  # the owning Headscale user's id — ownership checks compare THIS

    @property
    def ipv4(self) -> str | None:
        return next((ip for ip in self.ips if ":" not in ip), self.ips[0] if self.ips else None)

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        return self.expiry is not None and self.expiry <= (now or dt.datetime.now(dt.UTC))


@dataclass(frozen=True, slots=True)
class HsPreAuthKey:
    key: str
    expiration: dt.datetime | None


@dataclass(frozen=True, slots=True)
class HsKeyInfo:
    """A listed pre-auth key (the secret itself is masked by the server in listings)."""

    id: int
    user_id: int
    used: bool
    expiration: dt.datetime | None

    def is_pending(self, now: dt.datetime | None = None) -> bool:
        """Unused and not expired — could still register a device."""
        now = now or dt.datetime.now(dt.UTC)
        return not self.used and (self.expiration is None or self.expiration > now)


class HeadscaleApi(Protocol):
    """What LAN Play uses — :class:`HeadscaleClient`, or an in-memory fake in tests."""

    async def __aenter__(self) -> HeadscaleApi: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def health(self) -> bool: ...
    async def list_users(self, name: str | None = None) -> list[HsUser]: ...
    async def ensure_user(self, name: str) -> HsUser: ...
    async def create_preauth_key(
        self, user_id: int, *, expires_at: dt.datetime
    ) -> HsPreAuthKey: ...
    async def list_preauth_keys(self, user_id: int) -> list[HsKeyInfo]: ...
    async def expire_preauth_key(self, key_id: int) -> None: ...
    async def list_nodes(self, user: str | None = None) -> list[HsNode]: ...
    async def delete_node(self, node_id: int) -> None: ...
    async def expire_node(self, node_id: int) -> None: ...
    async def rename_node(self, node_id: int, name: str) -> None: ...
    async def get_policy(self) -> str: ...
    async def set_policy(self, policy: str) -> None: ...


def _user(raw: Any) -> HsUser:
    raw = raw if isinstance(raw, dict) else {}
    return HsUser(id=_id(raw.get("id")), name=str(raw.get("name") or ""))


def _node(raw: dict[str, Any]) -> HsNode:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    return HsNode(
        id=_id(raw.get("id")),
        # givenName is the DNS-safe name the tailnet shows; name is the raw hostname.
        name=str(raw.get("givenName") or raw.get("name") or ""),
        user=str((user or {}).get("name") or ""),
        ips=tuple(str(ip) for ip in raw.get("ipAddresses") or ()),
        online=bool(raw.get("online")),
        last_seen=_ts(raw.get("lastSeen")),
        expiry=_ts(raw.get("expiry")),
        created_at=_ts(raw.get("createdAt")),
        user_id=_id((user or {}).get("id")),
    )


class HeadscaleClient:
    """Async httpx client bound to one server + API key. Use as an async context manager or
    call :meth:`aclose`. HTTPS only — the API key travels in every request."""

    def __init__(
        self,
        login_server: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = _TIMEOUT,
    ) -> None:
        base = login_server.strip().rstrip("/")
        if not base.startswith("https://") or not api_key:
            raise HeadscaleError("headscale is not configured")
        self._http = httpx.AsyncClient(
            base_url=f"{base}/api/v1",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> HeadscaleClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with asyncio.timeout(CALL_DEADLINE):
                resp = await self._http.request(method, path, **kwargs)
        except TimeoutError as exc:
            log.warning("headscale call exceeded its deadline", method=method, path=path)
            raise HeadscaleError("headscale unreachable: deadline") from exc
        except httpx.HTTPError as exc:  # timeouts, DNS, refused, TLS
            log.warning("headscale unreachable", method=method, path=path, error=str(exc))
            raise HeadscaleError(f"headscale unreachable: {type(exc).__name__}") from exc
        status = resp.status_code
        if status in (401, 403):
            log.warning("headscale rejected the api key", method=method, path=path)
            raise HeadscaleAuthError(f"headscale rejected the api key ({status})", status=status)
        if status == 404:
            raise HeadscaleNotFound("headscale 404", status=status)
        if status >= 400:
            log.warning(
                "headscale request failed",
                method=method,
                path=path,
                status=status,
                body=resp.text[:500],
            )
            if path == "/policy" and _POLICY_READ_ONLY in resp.text:
                raise HeadscalePolicyReadOnly(
                    "headscale policy.mode is not database", status=status
                )
            raise HeadscaleError(f"headscale {status}", status=status)
        try:
            return resp.json() if resp.content else {}
        except ValueError as exc:
            raise HeadscaleError("headscale answered non-JSON", status=status) from exc

    # --- health ---------------------------------------------------------------
    async def health(self) -> bool:
        """``GET /health`` → its ``databaseConnectivity`` flag."""
        data = await self._request("GET", "/health")
        return bool(data.get("databaseConnectivity")) if isinstance(data, dict) else False

    # --- users ----------------------------------------------------------------
    async def list_users(self, name: str | None = None) -> list[HsUser]:
        params = {"name": name} if name else None
        data = await self._request("GET", "/user", params=params)
        users = [_user(u) for u in (data.get("users") or ())]
        # ``name`` is an exact filter server-side; re-check so a looser match never leaks in.
        return [u for u in users if not name or u.name == name]

    async def create_user(self, name: str) -> HsUser:
        data = await self._request("POST", "/user", json={"name": name})
        return _user(data.get("user"))

    async def ensure_user(self, name: str) -> HsUser:
        found = await self.list_users(name)
        return found[0] if found else await self.create_user(name)

    # --- pre-auth keys -----------------------------------------------------------
    async def create_preauth_key(self, user_id: int, *, expires_at: dt.datetime) -> HsPreAuthKey:
        """A single-use, non-ephemeral, tag-less key for ``user_id``."""
        body = {
            "user": str(user_id),
            "reusable": False,
            "ephemeral": False,
            "expiration": _utc_z(expires_at),
            "aclTags": [],
        }
        data = await self._request("POST", "/preauthkey", json=body)
        raw = data.get("preAuthKey") or {}
        key = str(raw.get("key") or "")
        if not key:
            raise HeadscaleError("headscale returned no pre-auth key")
        return HsPreAuthKey(key=key, expiration=_ts(raw.get("expiration")) or expires_at)

    async def list_preauth_keys(self, user_id: int) -> list[HsKeyInfo]:
        """Keys of the user with ``user_id`` (``GET /preauthkey`` takes no filter in v0.29)."""
        data = await self._request("GET", "/preauthkey")
        out: list[HsKeyInfo] = []
        for raw in data.get("preAuthKeys") or ():
            if not isinstance(raw, dict) or _user(raw.get("user")).id != user_id:
                continue
            out.append(
                HsKeyInfo(
                    id=_id(raw.get("id")),
                    user_id=user_id,
                    used=bool(raw.get("used")),
                    expiration=_ts(raw.get("expiration")),
                )
            )
        return out

    async def expire_preauth_key(self, key_id: int) -> None:
        await self._request("POST", "/preauthkey/expire", json={"id": str(int(key_id))})

    # --- nodes ----------------------------------------------------------------------
    async def list_nodes(self, user: str | None = None) -> list[HsNode]:
        params = {"user": user} if user else None
        data = await self._request("GET", "/node", params=params)
        return [_node(n) for n in (data.get("nodes") or ()) if isinstance(n, dict)]

    async def delete_node(self, node_id: int) -> None:
        await self._request("DELETE", f"/node/{int(node_id)}")

    async def expire_node(self, node_id: int) -> None:
        """Expire now (no ``expiry`` query = ``time.Now()`` server-side)."""
        await self._request("POST", f"/node/{int(node_id)}/expire")

    async def rename_node(self, node_id: int, name: str) -> None:
        await self._request("POST", f"/node/{int(node_id)}/rename/{name}")

    # --- policy ----------------------------------------------------------------------
    async def get_policy(self) -> str:
        data = await self._request("GET", "/policy")
        return str(data.get("policy") or "")

    async def set_policy(self, policy: str) -> None:
        await self._request("PUT", "/policy", json={"policy": policy})
