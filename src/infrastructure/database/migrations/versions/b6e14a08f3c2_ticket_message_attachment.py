"""ticket_messages.attachment_url/attachment_kind — screenshots in support tickets

Revision ID: b6e14a08f3c2
Revises: a3f7c92e5d16
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b6e14a08f3c2"
down_revision = "a3f7c92e5d16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket_messages", sa.Column("attachment_url", sa.String(length=255), nullable=True))
    op.add_column("ticket_messages", sa.Column("attachment_kind", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket_messages", "attachment_kind")
    op.drop_column("ticket_messages", "attachment_url")
