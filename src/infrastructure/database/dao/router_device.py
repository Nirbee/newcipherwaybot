"""RouterDevice DAO."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import func, or_, select

from src.core.enums import RouterDeviceStatus
from src.infrastructure.database.dao.base import BaseDAO
from src.infrastructure.database.models.router_device import RouterDevice


class RouterDeviceDAO(BaseDAO[RouterDevice]):
    model = RouterDevice

    async def by_token_hash(self, token_hash: str) -> RouterDevice | None:
        return await self.find_one(token_hash=token_hash)

    async def list_stale(self, older_than: dt.datetime) -> Sequence[RouterDevice]:
        """Devices due to flip OFFLINE: no heartbeat since ``older_than``, currently PENDING
        or ONLINE — already-OFFLINE/REVOKED devices need no further action."""
        stmt = select(RouterDevice).where(
            RouterDevice.status.in_((RouterDeviceStatus.PENDING, RouterDeviceStatus.ONLINE)),
            or_(RouterDevice.last_seen_at.is_(None), RouterDevice.last_seen_at < older_than),
        )
        return (await self.session.scalars(stmt)).all()

    async def host_assignment_counts(self, host_uuids: Sequence[str]) -> dict[str, int]:
        """How many non-revoked devices currently have each host as their primary — the
        least-connections signal for AUTO-assigning the next device. A device that's merely
        OFFLINE still occupies its slot (it's expected back); only REVOKED frees one."""
        counts = dict.fromkeys(host_uuids, 0)
        if not host_uuids:
            return counts
        stmt = (
            select(RouterDevice.primary_host_uuid, func.count())
            .where(
                RouterDevice.primary_host_uuid.in_(host_uuids),
                RouterDevice.status != RouterDeviceStatus.REVOKED,
            )
            .group_by(RouterDevice.primary_host_uuid)
        )
        for host_uuid, count in await self.session.execute(stmt):
            counts[host_uuid] = count
        return counts
