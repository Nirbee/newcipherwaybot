"""router devices (control plane for XKeen/Xray routers)

Revision ID: db42fabd5617
Revises: c8f21a5b9d34
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "db42fabd5617"
down_revision = "c8f21a5b9d34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "router_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "mode",
            sa.Enum("AUTO", "FORCE", name="routerdevicemode", native_enum=False, length=8),
            nullable=False,
            server_default="AUTO",
        ),
        sa.Column("primary_host_uuid", sa.String(length=64), nullable=True),
        sa.Column("backup_host_uuid", sa.String(length=64), nullable=True),
        sa.Column("config_etag", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "ONLINE", "OFFLINE", "REVOKED",
                name="routerdevicestatus", native_enum=False, length=16,
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("xray_version", sa.String(length=32), nullable=True),
        sa.Column("active_outbound", sa.String(length=64), nullable=True),
        sa.Column("external_ip", sa.String(length=45), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column(
            "install_report",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column("note", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_router_devices_token_hash", "router_devices", ["token_hash"], unique=True
    )
    op.create_index("ix_router_devices_subscription_id", "router_devices", ["subscription_id"])
    op.create_index("ix_router_devices_last_seen_at", "router_devices", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_router_devices_last_seen_at", table_name="router_devices")
    op.drop_index("ix_router_devices_subscription_id", table_name="router_devices")
    op.drop_index("ix_router_devices_token_hash", table_name="router_devices")
    op.drop_table("router_devices")
