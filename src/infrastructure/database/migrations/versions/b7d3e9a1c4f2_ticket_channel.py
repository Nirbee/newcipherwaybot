"""tickets.channel + tickets.subscription_id — separate support threads per channel

Revision ID: b7d3e9a1c4f2
Revises: f3b8d1c6a742
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7d3e9a1c4f2"
down_revision = "f3b8d1c6a742"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every existing ticket came from the bot or the mini-app/cabinet, which share one thread
    # per account — backfilled as BOT. SQLAlchemy's Enum(TicketChannel, ...) stores the member
    # NAME ("BOT"), same convention as plans.category / tickets.status.
    op.add_column(
        "tickets",
        sa.Column("channel", sa.String(length=16), nullable=False, server_default="BOT"),
    )
    # Windows-app threads are pinned to the subscription whose link authenticated the app.
    op.add_column("tickets", sa.Column("subscription_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_tickets_subscription_id_subscriptions",
        "tickets",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_tickets_subscription_id"), "tickets", ["subscription_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_tickets_subscription_id"), table_name="tickets")
    op.drop_constraint("fk_tickets_subscription_id_subscriptions", "tickets", type_="foreignkey")
    op.drop_column("tickets", "subscription_id")
    op.drop_column("tickets", "channel")
