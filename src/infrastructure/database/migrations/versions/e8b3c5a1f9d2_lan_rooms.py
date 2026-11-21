"""LAN Play: Headscale accounts, rooms, members, room bans

Revision ID: e8b3c5a1f9d2
Revises: d4f8a2c6e1b3
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e8b3c5a1f9d2"
down_revision = "d4f8a2c6e1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lan_rooms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "owner_subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("code", name="uq_lan_rooms_code"),
    )
    op.create_index("ix_lan_rooms_owner_subscription_id", "lan_rooms", ["owner_subscription_id"])
    op.create_table(
        "lan_room_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "room_id",
            sa.Integer(),
            sa.ForeignKey("lan_rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("room_id", "subscription_id", name="uq_lan_room_member"),
    )
    op.create_index("ix_lan_room_members_subscription_id", "lan_room_members", ["subscription_id"])
    op.create_table(
        "lan_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("hs_user", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("subscription_id", name="uq_lan_accounts_subscription_id"),
        sa.UniqueConstraint("hs_user", name="uq_lan_accounts_hs_user"),
    )
    op.create_table(
        "lan_room_bans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "room_id",
            sa.Integer(),
            sa.ForeignKey("lan_rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("room_id", "subscription_id", name="uq_lan_room_ban"),
    )
    op.create_index("ix_lan_room_bans_subscription_id", "lan_room_bans", ["subscription_id"])


def downgrade() -> None:
    op.drop_index("ix_lan_room_bans_subscription_id", table_name="lan_room_bans")
    op.drop_table("lan_room_bans")
    op.drop_table("lan_accounts")
    op.drop_index("ix_lan_room_members_subscription_id", table_name="lan_room_members")
    op.drop_table("lan_room_members")
    op.drop_index("ix_lan_rooms_owner_subscription_id", table_name="lan_rooms")
    op.drop_table("lan_rooms")
