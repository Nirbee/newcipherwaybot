"""LAN Play: per-subscription Headscale accounts, rooms, memberships and room bans.

The Headscale ACL policy is regenerated from these rows after every change (see
``src/infrastructure/services/lan.py``). Deleting a subscription cascades its account, its
memberships and the rooms it owns; the periodic LAN task then re-applies the policy and
expires the orphaned nodes.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.infrastructure.database.base import AwareDateTime, Base, IntPk


class LanRoom(IntPk, Base):
    __tablename__ = "lan_rooms"

    code: Mapped[str] = mapped_column(String(8), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    owner_subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        AwareDateTime, server_default=func.now(), nullable=False
    )


class LanRoomMember(IntPk, Base):
    __tablename__ = "lan_room_members"
    __table_args__ = (UniqueConstraint("room_id", "subscription_id", name="uq_lan_room_member"),)

    room_id: Mapped[int] = mapped_column(ForeignKey("lan_rooms.id", ondelete="CASCADE"))
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    joined_at: Mapped[dt.datetime] = mapped_column(
        AwareDateTime, server_default=func.now(), nullable=False
    )


class LanAccount(IntPk, Base):
    """The subscription's Headscale user. ``hs_user`` is random (``lan-<hex>``): the name is
    seen by every peer in the netmap, so it must not reveal the subscription id."""

    __tablename__ = "lan_accounts"

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), unique=True
    )
    hs_user: Mapped[str] = mapped_column(String(32), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        AwareDateTime, server_default=func.now(), nullable=False
    )


class LanRoomBan(IntPk, Base):
    """A subscription the owner kicked: it can't rejoin that room, even with a new code."""

    __tablename__ = "lan_room_bans"
    __table_args__ = (UniqueConstraint("room_id", "subscription_id", name="uq_lan_room_ban"),)

    room_id: Mapped[int] = mapped_column(ForeignKey("lan_rooms.id", ondelete="CASCADE"))
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        AwareDateTime, server_default=func.now(), nullable=False
    )
