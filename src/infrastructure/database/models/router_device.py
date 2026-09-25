"""RouterDevice — one physical router bound to an existing Subscription.

Deliberately NOT its own Remnawave panel user: a router is just another device sharing the
customer's existing subscription (traffic/device limits, expiry, squad) — the same subscription
their phone app may already use. This avoids a second panel identity per customer and the
"which subscription is current" conflict that a separate one would create (a customer buying a
router-capable plan would silently orphan whatever subscription they had before, since
``User.current_subscription_id`` holds only one at a time).

Host assignment (``primary_host_uuid`` / ``backup_host_uuid``) is populated differently
depending on ``mode``: AUTO has it set (and periodically rebalanced) by a least-connections
sweep over the admin's eligible-hosts allowlist; FORCE has it pinned by an admin and left alone
by that sweep. Either way the router's own config only ever gets these 1-2 hosts, not the whole
squad — client-side observatory/balancer picks the better of the two for latency/failover, the
server decides which few it gets to choose between at all (so the fleet doesn't herd onto
whichever host has the best ping for most of it).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.enums import RouterDeviceMode, RouterDeviceStatus
from src.infrastructure.database.base import AwareDateTime, Base, IntPk, JsonB, TimestampMixin


class RouterDevice(IntPk, TimestampMixin, Base):
    __tablename__ = "router_devices"

    # sha256 hex digest of the bearer token; the plain token is shown once at creation and
    # never stored — a DB leak must not hand out working device credentials.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(128))

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )

    mode: Mapped[RouterDeviceMode] = mapped_column(
        Enum(RouterDeviceMode, native_enum=False, length=8), default=RouterDeviceMode.AUTO
    )
    primary_host_uuid: Mapped[str | None] = mapped_column(String(64))
    backup_host_uuid: Mapped[str | None] = mapped_column(String(64))

    # Set after generating a config; compared against the agent's If-None-Match so an
    # unchanged config short-circuits to a 304 instead of resending the same JSON every poll.
    config_etag: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[RouterDeviceStatus] = mapped_column(
        Enum(RouterDeviceStatus, native_enum=False, length=16), default=RouterDeviceStatus.PENDING
    )
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(AwareDateTime, index=True)
    xray_version: Mapped[str | None] = mapped_column(String(32))
    active_outbound: Mapped[str | None] = mapped_column(String(64))
    external_ip: Mapped[str | None] = mapped_column(String(45))  # IPv6-max length
    last_error: Mapped[str | None] = mapped_column(String(512))
    install_report: Mapped[dict[str, Any] | None] = mapped_column(JsonB)
    # Latest self-report from the agent's heartbeat (XKeen ports, routeOnly, DNS probe, cron).
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JsonB)

    note: Mapped[str | None] = mapped_column(String(512))
