"""RemnawaveService — business-facing panel operations over the RemnawaveClient protocol.

Handles version capability probing, unit conversion (GB->bytes, unlimited modelling) and
username templating with the permanent ``short_id`` suffix. No httpx here — that's the
concrete client's job.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from src.application.common.panel import RemnawaveClient
from src.application.dto.panel import PanelRef, PanelUser, PanelVersion, ProvisionSpec
from src.core.constants import (
    BYTES_PER_GB,
    MIN_REMNAWAVE_VERSION,
    PANEL_USERNAME_MAX,
    PANEL_USERNAME_PREFIX,
    UNLIMITED_EXPIRE_DAYS,
    UNLIMITED_TRAFFIC_BYTES,
)
from src.core.exceptions import RemnawaveError, RemnawaveVersionError
from src.core.logging import get_logger

_UNKNOWN_VERSION = (0, 0, 0)
log = get_logger(__name__)


def _already_in_state(exc: Exception, state: str) -> bool:
    """True when the panel refused a no-op toggle: 400 «User already enabled/disabled» (A030).

    For an idempotent enable/disable that rejection IS the desired end state, not a failure —
    treating it as an error aborted grace mid-way (panel updated, local row not), which made the
    sweep re-apply the panel side every run.
    """
    return f"already {state}" in str(exc).lower()


class RemnawaveService:
    def __init__(self, client: RemnawaveClient) -> None:
        self._client = client

    async def ensure_supported(self) -> PanelVersion:
        """Probe the panel version at startup.

        Only hard-fails when the version is *known* and below the minimum. Some panels do
        not expose their version via the API — in that case we proceed with a warning rather
        than block a healthy panel (verified against a live panel with no version field).
        """
        version = await self._client.get_version()
        if version.tuple == _UNKNOWN_VERSION:
            log.warning("panel_version_unknown", note="proceeding without a version gate")
            return version
        if version.tuple < MIN_REMNAWAVE_VERSION:
            raise RemnawaveVersionError(
                f"panel {version.raw} < required {'.'.join(map(str, MIN_REMNAWAVE_VERSION))}"
            )
        return version

    @staticmethod
    def gb_to_bytes(gb: int | None) -> int:
        """Convert a GB limit to bytes; None/0 -> unlimited (0)."""
        if not gb:
            return UNLIMITED_TRAFFIC_BYTES
        return gb * BYTES_PER_GB

    @staticmethod
    def username_for(short_id: str, *, prefix: str = PANEL_USERNAME_PREFIX) -> str:
        return f"{prefix}{short_id}"[:PANEL_USERNAME_MAX]

    def build_spec(
        self,
        *,
        short_id: str,
        telegram_id: int | None,
        expire_at: dt.datetime,
        traffic_limit_bytes: int,
        device_limit: int | None,
        internal_squads: tuple[str, ...] = (),
        external_squad: str | None = None,
        unlimited_expire: bool = False,
        tag: str | None = None,
        description: str | None = None,
    ) -> ProvisionSpec:
        """``description`` overrides the default ``tg:<id>`` (a hand-added client carries their
        name there); like ``tag`` it is only sent when set, so updates leave it alone."""
        if unlimited_expire:
            expire_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=UNLIMITED_EXPIRE_DAYS)
        return ProvisionSpec(
            short_id=short_id,
            telegram_id=telegram_id,
            username=self.username_for(short_id),
            expire_at=expire_at,
            traffic_limit_bytes=traffic_limit_bytes,
            device_limit=device_limit,
            internal_squads=internal_squads,
            external_squad=external_squad,
            description=description or (f"tg:{telegram_id}" if telegram_id else None),
            tag=tag,
        )

    async def provision(self, spec: ProvisionSpec) -> PanelUser:
        """Create the panel user for a subscription (panel-first, ADR-0005)."""
        return await self._client.create_user(spec)

    async def discard(self, ref: PanelRef) -> None:
        """Best effort: remove a panel user this very request just created and could not
        record (its local save failed) — never leave an orphan nobody pays for."""
        try:
            await self._client.delete_user(ref)
        except Exception as exc:
            log.warning("panel: orphan not removed", ref=str(ref), error=str(exc))

    async def panel_expiry(self, ref: PanelRef) -> dt.datetime | None:
        """The panel user's current expiry, or None when the panel didn't answer / has none."""
        try:
            user = await self._client.get_user(ref)
        except Exception as exc:
            log.warning("panel expiry unknown", ref=str(ref), error=str(exc))
            return None
        if user is None or user.expire_at is None:
            return None
        value = user.expire_at
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)

    async def keep_panel_expiry(self, ref: PanelRef, spec: ProvisionSpec) -> ProvisionSpec:
        """Never move the panel's expiry BACK: an owner who extended someone directly in the
        panel (MultiScript) must not lose that when the bot pushes its own, older date."""
        if spec.only is not None and "expireAt" not in spec.only:
            return spec  # a partial update that doesn't touch the expiry
        current = await self.panel_expiry(ref)
        if current is None or current <= spec.expire_at:
            return spec
        log.info(
            "panel expiry kept",
            ref=str(ref),
            bot=spec.expire_at.isoformat(),
            panel=current.isoformat(),
        )
        return replace(spec, expire_at=current)

    async def apply(
        self, ref: PanelRef, spec: ProvisionSpec, *, allow_shorten: bool = False
    ) -> PanelUser:
        """Push a spec change (renew/change) to an existing panel user. The expiry never goes
        earlier than the panel's current one unless ``allow_shorten`` (an admin's explicit «до
        даты», a grace window's deliberately short expiry)."""
        if not allow_shorten:
            spec = await self.keep_panel_expiry(ref, spec)
        return await self._client.update_user(ref, spec)

    async def enable(self, ref: PanelRef) -> None:
        """Re-enable a disabled panel user (grace entry / self-heal).

        Idempotent: pushing a future expiry already flips the panel user back to ACTIVE, so the
        panel then answers 400 «User already enabled» — the state we wanted anyway. Any other
        panel error propagates.
        """
        try:
            await self._client.enable_user(ref)
        except RemnawaveError as exc:
            if not _already_in_state(exc, "enabled"):
                raise

    async def disable(self, ref: PanelRef) -> None:
        """Disable a panel user (grace expiry — kill access without deleting).

        Idempotent for the same reason as :meth:`enable`: a user the panel already disabled
        (e.g. by expiry) must not abort the grace sweep.
        """
        try:
            await self._client.disable_user(ref)
        except RemnawaveError as exc:
            if not _already_in_state(exc, "disabled"):
                raise

    async def reset_traffic(self, ref: PanelRef) -> None:
        """Zero the used-traffic counter so a fresh grace allowance is actually usable."""
        await self._client.reset_traffic(ref)
