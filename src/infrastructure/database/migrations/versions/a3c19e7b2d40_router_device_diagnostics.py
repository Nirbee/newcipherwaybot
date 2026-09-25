"""router_devices.diagnostics (agent self-report shown in the admin panel)

Revision ID: a3c19e7b2d40
Revises: 8f58f06c6f52
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3c19e7b2d40"
down_revision = "8f58f06c6f52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "router_devices",
        sa.Column(
            "diagnostics",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("router_devices", "diagnostics")
