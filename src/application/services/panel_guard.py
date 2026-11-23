"""Service accounts of MultiScript in the panel — the bot never writes to them.

MultiScript (the panel's control layer) keeps its own users in Remnawave: the cascade's relay
client (``cascade_relay_<hex>``, ~10-year expiry) and a status monitor (``ms-status-monitor``).
The panel import once adopted both into the bot, and from then on every bot sweep (resync,
grace, device guard, reconcile, admin actions) could have disabled, renewed or deleted them —
breaking the cascade. The list of such usernames is a setting (``PANEL_SERVICE_USERNAMES``,
``*`` wildcards allowed).

:class:`GuardedPanelClient` wraps the panel client for the whole app: reads pass through,
every write aimed at a service account raises :class:`PanelServiceAccountError` — whatever
path it came from, so no caller can forget the check. A target is recognised by

* the bot's own record: subscriptions whose ``panel_username`` matches (loaded from the DB,
  refreshed every minute — works even when the panel can't be asked), or
* the panel's answer: usernames seen in any read are remembered (a panel username never
  changes), an unknown target is looked up once with ``get_user_strict`` before the write.
  If the panel can't answer that lookup, the write is refused (fail-closed); only an explicit
  «no such user» lets it through.

The setting is always merged with a hard minimum in code (:data:`HARD_SERVICE_USERNAMES`):
clearing it never switches the protection off. Saving it is OWNER-only and rejects patterns
broad enough to catch ordinary customers (``*``, ``s*`` …), see :func:`validate_patterns`.
"""

from __future__ import annotations

import fnmatch
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from src.application.common.panel import RemnawaveClient
from src.application.dto.panel import (
    PanelDevice,
    PanelHost,
    PanelNode,
    PanelRef,
    PanelSquad,
    PanelUser,
    PanelUserRef,
    PanelVersion,
    ProvisionSpec,
)
from src.core.exceptions import RemnawaveError
from src.core.logging import get_logger

log = get_logger(__name__)

# Always protected, whatever the setting says.
HARD_SERVICE_USERNAMES: tuple[str, ...] = (
    "cascade_relay_b74178",
    "ms-status-monitor",
    "cascade_relay_*",
)
DEFAULT_SERVICE_USERNAMES = ", ".join(HARD_SERVICE_USERNAMES)
SERVICE_SETTING = "PANEL_SERVICE_USERNAMES"
SERVICE_ACCOUNT_REFUSAL = "служебная учётка MultiScript — бот её не меняет"
# Names ordinary customers have on the panel: a pattern matching any of them would block
# renewals after payment, so it can't be saved.
_CUSTOMER_PROBES = ("sub_x", "sub_abc123", "user_1", "user_5072755730", "nl-017", "a", "1")
# A literal prefix of 3+ characters starting with a letter; wildcards only after it.
_PATTERN = re.compile(r"^[a-z][a-z0-9_.\-]{2,}[a-z0-9_.\-*?]*$")


class PanelServiceAccountError(RemnawaveError):
    """A panel write was refused: the target is a MultiScript service account."""


def _split(raw: str | None) -> list[str]:
    return [p.strip().lower() for p in re.split(r"[,;\s]+", raw or "") if p.strip()]


def parse_patterns(raw: str | None) -> tuple[str, ...]:
    """``"a, b_*  c"`` -> the hard minimum + ``("a", "b_*", "c")`` (lower-cased; commas,
    spaces or ``;``). An empty setting still yields the hard minimum."""
    return tuple(dict.fromkeys([*HARD_SERVICE_USERNAMES, *_split(raw)]))


def validate_patterns(raw: str | None) -> None:
    """Refuse patterns that could catch real customers: a bare wildcard, one that doesn't
    start with a letter, or any matching a customer-style name (``sub_x``, ``user_1``…)."""
    for pattern in _split(raw):
        if not _PATTERN.match(pattern) or not pattern.rstrip("*?"):
            raise ValueError(
                f"«{pattern}»: нужно минимум три обычных символа в начале, первый — буква "
                "(латиница, цифры, _ - .); * и ? — только после них"
            )
        caught = [n for n in _CUSTOMER_PROBES if fnmatch.fnmatchcase(n, pattern)]
        if caught:
            raise ValueError(
                f"«{pattern}» слишком широкий: под него попадают обычные клиенты ({caught[0]})"
            )


def settings_guard(changes: dict[str, Any]) -> None:
    """PATCH /settings: a new list must be safe (who may change it is checked by the route)."""
    if SERVICE_SETTING in changes:
        validate_patterns(str(changes[SERVICE_SETTING] or ""))


def is_service_username(name: str | None, patterns: Iterable[str]) -> bool:
    value = (name or "").strip().lower()
    return bool(value) and any(fnmatch.fnmatchcase(value, p) for p in patterns)


def ref_keys(ref: PanelRef) -> set[str]:
    """Keys a write target is known by; the same shape as ``panel_import`` uses."""
    if not isinstance(ref, PanelUserRef):
        return {f"uuid:{ref}"}
    keys: set[str] = set()
    if ref.panel_id is not None:
        keys.add(f"id:{ref.panel_id}")
    if ref.uuid is not None:
        keys.add(f"uuid:{ref.uuid}")
    if ref.short_id:
        keys.add(f"code:{ref.short_id}")
    return keys


def user_keys(user: PanelUser) -> set[str]:
    keys: set[str] = set()
    if user.panel_id is not None:
        keys.add(f"id:{user.panel_id}")
    if user.uuid is not None:
        keys.add(f"uuid:{user.uuid}")
    if user.short_id:
        keys.add(f"code:{user.short_id}")
    return keys


# () -> (patterns, keys of subscriptions the bot itself knows are service accounts)
ProtectionLoader = Callable[[], Awaitable[tuple[tuple[str, ...], frozenset[str]]]]


class GuardedPanelClient:
    """``RemnawaveClient`` decorator refusing every write to a service account."""

    RELOAD_SECONDS = 60.0

    def __init__(self, inner: RemnawaveClient, loader: ProtectionLoader | None = None) -> None:
        self._inner = inner
        self._loader = loader
        self._patterns = parse_patterns(DEFAULT_SERVICE_USERNAMES)
        self._protected: frozenset[str] = frozenset()
        self._loaded_at: float | None = None
        self._names: dict[str, str] = {}  # panel key -> username (immutable on the panel)

    @property
    def inner(self) -> RemnawaveClient:
        return self._inner

    # --- protection -----------------------------------------------------------------------

    async def _protection(self) -> tuple[tuple[str, ...], frozenset[str]]:
        now = time.monotonic()
        if self._loader is not None and (
            self._loaded_at is None or now - self._loaded_at >= self.RELOAD_SECONDS
        ):
            try:
                self._patterns, self._protected = await self._loader()
            except Exception as exc:  # DB hiccup: keep the last known list, never fail open
                log.warning("panel guard: service accounts not reloaded", error=str(exc))
            self._loaded_at = now
        return self._patterns, self._protected

    def _learn(self, user: PanelUser | None) -> PanelUser | None:
        if user is not None and user.username:
            for key in user_keys(user):
                self._names[key] = user.username
        return user

    async def is_service(self, ref: PanelRef) -> bool:
        """Whether ``ref`` points at a service account (may ask the panel once)."""
        patterns, protected = await self._protection()
        keys = ref_keys(ref)
        if keys & protected:
            return True
        name = next((self._names[k] for k in keys if k in self._names), None)
        if name is None:
            # Fail-closed: a panel that is down / rejects us raises here and the write with
            # it; only an explicit «no such user» (None) lets an unknown target through.
            user = self._learn(await self._inner.get_user_strict(ref))
            name = user.username if user is not None else None
        return is_service_username(name, patterns)

    async def _check(self, ref: PanelRef, action: str) -> None:
        if await self.is_service(ref):
            log.warning("panel write refused: service account", action=action, ref=str(ref))
            raise PanelServiceAccountError(SERVICE_ACCOUNT_REFUSAL)

    # --- reads (pass through, remembering usernames) ----------------------------------------

    async def get_version(self) -> PanelVersion:
        return await self._inner.get_version()

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        return self._learn(await self._inner.get_user(ref))

    async def get_user_strict(self, ref: PanelRef) -> PanelUser | None:
        return self._learn(await self._inner.get_user_strict(ref))

    async def get_user_by_telegram_id(self, telegram_id: int) -> PanelUser | None:
        return self._learn(await self._inner.get_user_by_telegram_id(telegram_id))

    async def get_user_by_short_uuid(self, short_uuid: str) -> PanelUser | None:
        return self._learn(await self._inner.get_user_by_short_uuid(short_uuid))

    async def list_users(self) -> list[PanelUser]:
        users = await self._inner.list_users()
        for user in users:
            self._learn(user)
        return users

    async def list_hwid_devices(self) -> list[PanelDevice]:
        return await self._inner.list_hwid_devices()

    async def get_devices(self, ref: PanelRef) -> list[PanelDevice]:
        return await self._inner.get_devices(ref)

    async def start_users_ips_job(self, node_uuid: str) -> str:
        return await self._inner.start_users_ips_job(node_uuid)

    async def get_users_ips_result(self, job_id: str) -> list[tuple[str, list[str]]] | None:
        return await self._inner.get_users_ips_result(job_id)

    async def get_internal_squads(self) -> list[PanelSquad]:
        return await self._inner.get_internal_squads()

    async def get_nodes(self) -> list[PanelNode]:
        return await self._inner.get_nodes()

    async def get_hosts(self) -> list[PanelHost]:
        return await self._inner.get_hosts()

    async def fetch_subscription_json(self, subscription_url: str) -> Any:
        return await self._inner.fetch_subscription_json(subscription_url)

    async def get_subscription_page_config(self) -> dict[str, Any] | None:
        return await self._inner.get_subscription_page_config()

    # --- writes (refused for service accounts) ----------------------------------------------

    async def create_user(self, spec: ProvisionSpec) -> PanelUser:
        # The bot names its users itself (sub_<short_id>); refuse anything else that looks
        # like a service account, so the bot can't create (or collide with) one.
        patterns, _ = await self._protection()
        if is_service_username(spec.username, patterns):
            log.warning("panel create refused: service account name", username=spec.username)
            raise PanelServiceAccountError(SERVICE_ACCOUNT_REFUSAL)
        user = await self._inner.create_user(spec)
        self._learn(user)
        return user

    async def update_user(self, ref: PanelRef, spec: ProvisionSpec) -> PanelUser:
        await self._check(ref, "update")
        user = await self._inner.update_user(ref, spec)
        self._learn(user)
        return user

    async def enable_user(self, ref: PanelRef) -> None:
        await self._check(ref, "enable")
        await self._inner.enable_user(ref)

    async def disable_user(self, ref: PanelRef) -> None:
        await self._check(ref, "disable")
        await self._inner.disable_user(ref)

    async def delete_user(self, ref: PanelRef) -> None:
        await self._check(ref, "delete")
        await self._inner.delete_user(ref)

    async def reset_traffic(self, ref: PanelRef) -> None:
        await self._check(ref, "reset_traffic")
        await self._inner.reset_traffic(ref)

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        await self._check(ref, "revoke")
        user = await self._inner.revoke_subscription(ref)
        self._learn(user)
        return user

    async def drop_connections(self, ref: PanelRef) -> None:
        await self._check(ref, "drop_connections")
        await self._inner.drop_connections(ref)

    async def delete_device(self, ref: PanelRef, hwid: str) -> None:
        await self._check(ref, "delete_device")
        await self._inner.delete_device(ref, hwid)

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()
