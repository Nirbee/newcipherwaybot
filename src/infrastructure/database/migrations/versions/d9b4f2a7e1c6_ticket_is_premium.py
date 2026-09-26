"""tickets.is_premium (premium server requests / premium-plan customers)

Revision ID: d9b4f2a7e1c6
Revises: c5e8a1f0b3d2
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d9b4f2a7e1c6"
down_revision = "c5e8a1f0b3d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("is_premium", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tickets", "is_premium")
