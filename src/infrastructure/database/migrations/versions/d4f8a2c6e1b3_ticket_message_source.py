"""ticket_messages.source — where each customer message was written (bot / mini-app / site / app)

Revision ID: d4f8a2c6e1b3
Revises: c9e4a7d2b1f6
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4f8a2c6e1b3"
down_revision = "c9e4a7d2b1f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: staff/AI messages have no source. Stored as the TicketSource member NAME
    # (BOT/MINIAPP/WEB/APP), same convention as tickets.channel / ticket_messages.author.
    op.add_column("ticket_messages", sa.Column("source", sa.String(length=16), nullable=True))
    # Backfill customer messages by their thread's channel: APP is the Windows app; CABINET
    # tickets could only come from the mini-app (the web cabinet had no support chat before
    # this revision); everything else is the bot. Pre-b7d3e9a1c4f2 mini-app tickets were
    # already folded into BOT then — that history is not recoverable.
    op.execute(
        """
        UPDATE ticket_messages SET source = CASE (
            SELECT tickets.channel FROM tickets WHERE tickets.id = ticket_messages.ticket_id
        )
            WHEN 'APP' THEN 'APP'
            WHEN 'CABINET' THEN 'MINIAPP'
            ELSE 'BOT'
        END
        WHERE author = 'USER'
        """
    )


def downgrade() -> None:
    op.drop_column("ticket_messages", "source")
