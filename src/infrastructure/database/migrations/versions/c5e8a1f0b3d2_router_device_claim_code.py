"""router_devices.claim_code (customer QR onboarding)

Revision ID: c5e8a1f0b3d2
Revises: a3c19e7b2d40
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c5e8a1f0b3d2"
down_revision = "a3c19e7b2d40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("router_devices", sa.Column("claim_code", sa.String(32), nullable=True))
    op.create_index(
        "ix_router_devices_claim_code", "router_devices", ["claim_code"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_router_devices_claim_code", table_name="router_devices")
    op.drop_column("router_devices", "claim_code")
