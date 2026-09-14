"""users.allowed_admin_screens (scoped staff accounts)

Revision ID: 8f58f06c6f52
Revises: db42fabd5617
Create Date: 2026-09-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "8f58f06c6f52"
down_revision = "db42fabd5617"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "allowed_admin_screens",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "allowed_admin_screens")
