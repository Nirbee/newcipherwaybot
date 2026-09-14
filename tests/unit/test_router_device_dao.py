"""RouterDeviceDAO: staleness sweep query + least-connections host-assignment counts."""

from __future__ import annotations

import datetime as dt

from src.core.enums import RouterDeviceStatus
from src.infrastructure.database.models.router_device import RouterDevice
from src.infrastructure.database.uow import UnitOfWork


async def test_list_stale_only_flips_online_past_cutoff(uow: UnitOfWork) -> None:
    """PENDING (never checked in) and already-OFFLINE/REVOKED must NOT come back — only an
    ONLINE device whose heartbeat has actually gone stale."""
    async with uow:
        now = dt.datetime.now(dt.UTC)
        stale_online = RouterDevice(
            token_hash="a" * 64, label="stale", subscription_id=1,
            status=RouterDeviceStatus.ONLINE, last_seen_at=now - dt.timedelta(minutes=30),
        )
        fresh_online = RouterDevice(
            token_hash="b" * 64, label="fresh", subscription_id=1,
            status=RouterDeviceStatus.ONLINE, last_seen_at=now - dt.timedelta(minutes=1),
        )
        never_checked_in = RouterDevice(
            token_hash="c" * 64, label="pending", subscription_id=1,
            status=RouterDeviceStatus.PENDING, last_seen_at=None,
        )
        already_offline = RouterDevice(
            token_hash="d" * 64, label="offline", subscription_id=1,
            status=RouterDeviceStatus.OFFLINE, last_seen_at=now - dt.timedelta(hours=5),
        )
        revoked = RouterDevice(
            token_hash="e" * 64, label="revoked", subscription_id=1,
            status=RouterDeviceStatus.REVOKED, last_seen_at=now - dt.timedelta(hours=5),
        )
        for d in (stale_online, fresh_online, never_checked_in, already_offline, revoked):
            await uow.router_devices.add(d)
        await uow.commit()

        cutoff = now - dt.timedelta(minutes=15)
        stale = await uow.router_devices.list_stale(cutoff)
        assert [d.id for d in stale] == [stale_online.id]


async def test_host_assignment_counts_ignores_revoked_and_unlisted_hosts(
    uow: UnitOfWork,
) -> None:
    async with uow:
        a1 = RouterDevice(
            token_hash="f" * 64, label="a1", subscription_id=1, primary_host_uuid="host-a",
        )
        a2 = RouterDevice(
            token_hash="g" * 64, label="a2", subscription_id=1, primary_host_uuid="host-a",
        )
        b1 = RouterDevice(
            token_hash="h" * 64, label="b1", subscription_id=1, primary_host_uuid="host-b",
        )
        revoked_on_a = RouterDevice(
            token_hash="i" * 64, label="a-revoked", subscription_id=1,
            primary_host_uuid="host-a", status=RouterDeviceStatus.REVOKED,
        )
        for d in (a1, a2, b1, revoked_on_a):
            await uow.router_devices.add(d)
        await uow.commit()

        counts = await uow.router_devices.host_assignment_counts(["host-a", "host-b", "host-c"])
        assert counts == {"host-a": 2, "host-b": 1, "host-c": 0}


async def test_host_assignment_counts_empty_input_returns_empty_dict(uow: UnitOfWork) -> None:
    async with uow:
        assert await uow.router_devices.host_assignment_counts([]) == {}
