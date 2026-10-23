"""admin_api_keys (read-only admin API keys for AI agents)

Revision ID: f3b8d1c6a742
Revises: e2a7c4d9f5b1
Create Date: 2026-10-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f3b8d1c6a742"
down_revision = "e2a7c4d9f5b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "allowed_screens",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )
    op.create_index("ix_admin_api_keys_key_hash", "admin_api_keys", ["key_hash"], unique=True)
    op.create_index("ix_admin_api_keys_created_by", "admin_api_keys", ["created_by"])


def downgrade() -> None:
    op.drop_table("admin_api_keys")
