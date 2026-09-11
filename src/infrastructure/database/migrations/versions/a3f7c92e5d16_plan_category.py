"""plans.category — app vs router storefront bucket

Revision ID: a3f7c92e5d16
Revises: d5b1e93c47af
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f7c92e5d16"
down_revision = "d5b1e93c47af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default backfills every existing plan as APP (today's only category) so the
    # column can be NOT NULL from the start; the default is dropped after backfill so future
    # inserts must go through the ORM default instead of silently relying on the DB one.
    # NOTE: SQLAlchemy's Enum(PlanCategory, ...) stores/validates the member NAME ("APP"),
    # not its .value ("app") — same convention as every other Enum column in this codebase
    # (see e.g. ticketstatus: "OPEN"/"WAITING"/"CLOSED" in the initial schema migration).
    op.add_column(
        "plans",
        sa.Column("category", sa.String(length=16), nullable=False, server_default="APP"),
    )
    op.alter_column("plans", "category", server_default=None)


def downgrade() -> None:
    op.drop_column("plans", "category")
