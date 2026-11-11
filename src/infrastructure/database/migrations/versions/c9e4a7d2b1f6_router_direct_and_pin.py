"""router_devices: «Напрямую» (direct_until/direct_forever), location pin, delivered revision

Revision ID: c9e4a7d2b1f6
Revises: b7d3e9a1c4f2
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9e4a7d2b1f6"
down_revision = "b7d3e9a1c4f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All nullable / defaulted: an existing router stays exactly as it was (VPN on, «Авто»).
    op.add_column(
        "router_devices",
        sa.Column("direct_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "router_devices",
        sa.Column("direct_forever", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "router_devices",
        sa.Column("pinned_host_uuid", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "router_devices",
        sa.Column("config_rev", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("router_devices", "config_rev")
    op.drop_column("router_devices", "pinned_host_uuid")
    op.drop_column("router_devices", "direct_forever")
    op.drop_column("router_devices", "direct_until")
