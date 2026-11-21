"""LAN Play room DAOs."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, select

from src.application.services.lan import new_headscale_user
from src.infrastructure.database.dao.base import BaseDAO
from src.infrastructure.database.models.lan_room import (
    LanAccount,
    LanRoom,
    LanRoomBan,
    LanRoomMember,
)


class LanRoomDAO(BaseDAO[LanRoom]):
    model = LanRoom

    async def by_code(self, code: str) -> LanRoom | None:
        return await self.find_one(code=code)

    async def for_subscription(self, subscription_id: int) -> Sequence[LanRoom]:
        """Rooms the subscription is a member of (owned ones included), oldest first."""
        stmt = (
            select(LanRoom)
            .join(LanRoomMember, LanRoomMember.room_id == LanRoom.id)
            .where(LanRoomMember.subscription_id == subscription_id)
            .order_by(LanRoom.id)
        )
        return (await self.session.scalars(stmt)).all()

    async def remove(self, room: LanRoom) -> None:
        """Delete a room with its memberships (explicitly — sqlite tests don't cascade)."""
        await self.session.execute(delete(LanRoomMember).where(LanRoomMember.room_id == room.id))
        await self.session.execute(delete(LanRoomBan).where(LanRoomBan.room_id == room.id))
        await self.session.delete(room)
        await self.session.flush()

    async def with_member_counts(self) -> list[tuple[LanRoom, int]]:
        stmt = (
            select(LanRoom, func.count(LanRoomMember.id))
            .outerjoin(LanRoomMember, LanRoomMember.room_id == LanRoom.id)
            .group_by(LanRoom.id)
            .order_by(LanRoom.id.desc())
        )
        return [(room, int(n)) for room, n in (await self.session.execute(stmt)).all()]


class LanRoomMemberDAO(BaseDAO[LanRoomMember]):
    model = LanRoomMember

    async def of_room(self, room_id: int) -> Sequence[LanRoomMember]:
        """Members in join order (the order seat labels «Игрок N» follow)."""
        stmt = select(LanRoomMember).where(LanRoomMember.room_id == room_id)
        return (await self.session.scalars(stmt.order_by(LanRoomMember.id))).all()

    async def all_by_room(self) -> dict[int, list[str]]:
        """room id → members' Headscale user names, for regenerating the whole policy.
        Members without an account yet have no devices — nothing to allow for them."""
        rows = (
            await self.session.execute(
                select(LanRoomMember.room_id, LanAccount.hs_user)
                .join(LanAccount, LanAccount.subscription_id == LanRoomMember.subscription_id)
                .order_by(LanRoomMember.room_id, LanRoomMember.id)
            )
        ).all()
        out: dict[int, list[str]] = {}
        for room_id, hs_user in rows:
            out.setdefault(int(room_id), []).append(str(hs_user))
        return out

    async def memberships(self, subscription_id: int) -> int:
        return await self.count(subscription_id=subscription_id)

    async def count_in(self, subscription_ids: set[int]) -> int:
        stmt = select(func.count()).where(LanRoomMember.subscription_id.in_(subscription_ids))
        return int(await self.session.scalar(stmt) or 0)


class LanRoomBanDAO(BaseDAO[LanRoomBan]):
    model = LanRoomBan

    async def is_banned(self, room_id: int, subscription_id: int) -> bool:
        return await self.find_one(room_id=room_id, subscription_id=subscription_id) is not None


class LanAccountDAO(BaseDAO[LanAccount]):
    model = LanAccount

    async def ensure(self, subscription_id: int) -> LanAccount:
        """The subscription's account, created with a fresh random Headscale name. Callers
        hold the LAN policy lock, so two requests can't create it twice."""
        found = await self.find_one(subscription_id=subscription_id)
        if found is not None:
            return found
        for _ in range(5):
            name = new_headscale_user()
            if await self.find_one(hs_user=name) is None:
                return await self.add(LanAccount(subscription_id=subscription_id, hs_user=name))
        raise RuntimeError("could not generate a free LAN account name")

    async def hs_user_of(self, subscription_id: int) -> str | None:
        found = await self.find_one(subscription_id=subscription_id)
        return found.hs_user if found is not None else None

    async def by_hs_users(self, names: Sequence[str]) -> dict[str, int]:
        """Headscale user name → subscription id (only the names we know)."""
        if not names:
            return {}
        rows = await self.session.execute(
            select(LanAccount.hs_user, LanAccount.subscription_id).where(
                LanAccount.hs_user.in_(list(names))
            )
        )
        return {str(n): int(s) for n, s in rows.all()}
