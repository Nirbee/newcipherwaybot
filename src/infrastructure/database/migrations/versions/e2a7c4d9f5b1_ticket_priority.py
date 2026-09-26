"""tickets.priority (manual support priority)

Revision ID: e2a7c4d9f5b1
Revises: d9b4f2a7e1c6
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2a7c4d9f5b1"
down_revision = "d9b4f2a7e1c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("tickets", "priority")
