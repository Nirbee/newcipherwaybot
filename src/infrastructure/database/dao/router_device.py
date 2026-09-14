"""RouterDevice DAO."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import func, select

from src.core.enums import RouterDeviceStatus
from src.infrastructure.database.dao.base import BaseDAO
from src.infrastructure.database.models.router_device import RouterDevice


class RouterDeviceDAO(BaseDAO[RouterDevice]):
    model = RouterDevice

    async def by_token_hash(self, token_hash: str) -> RouterDevice | None:
        return await self.find_one(token_hash=token_hash)

    async def list_stale(self, older_than: dt.datetime) -> Sequence[RouterDevice]:
        """ONLINE devices whose last heartbeat is older than ``older_than`` — due to flip
        OFFLINE. PENDING is deliberately excluded: it means "never checked in yet" (created but
        not installed), which is normal and can last arbitrarily long — flipping it to OFFLINE
        would wrongly read as "was working, now isn't" to an admin. Already-OFFLINE/REVOKED
        need no further action."""
        stmt = select(RouterDevice).where(
            RouterDevice.status == RouterDeviceStatus.ONLINE,
            RouterDevice.last_seen_at < older_than,
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
