"""DevVersion: diagnostics reports from the Windows app

Revision ID: a6d2f8c4e1b9
Revises: e8b3c5a1f9d2
Create Date: 2026-11-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a6d2f8c4e1b9"
down_revision = "e8b3c5a1f9d2"
branch_labels = None
depends_on = None

_JSON = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "devversion_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("install_id", sa.String(length=36), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("app_version", sa.String(length=32), nullable=False),
        sa.Column("consent_version", sa.String(length=8), nullable=False),
        sa.Column("schema", sa.Integer(), nullable=False),
        sa.Column("asn", sa.BigInteger(), nullable=True),
        sa.Column("as_org", sa.String(length=200), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("region", sa.String(length=100), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("digest", _JSON, nullable=False),
    )
    op.create_index(
        "ix_devversion_reports_subscription_id", "devversion_reports", ["subscription_id"]
    )
    op.create_index("ix_devversion_reports_received_at", "devversion_reports", ["received_at"])
    op.create_index("ix_devversion_reports_asn", "devversion_reports", ["asn"])


def downgrade() -> None:
    op.drop_index("ix_devversion_reports_asn", table_name="devversion_reports")
    op.drop_index("ix_devversion_reports_received_at", table_name="devversion_reports")
    op.drop_index("ix_devversion_reports_subscription_id", table_name="devversion_reports")
    op.drop_table("devversion_reports")
