"""Fix plans.category stored as lowercase — must be the Enum member NAME, not .value

a3f7c92e5d16 backfilled every plan with "app" (lowercase), but SQLAlchemy's
Enum(PlanCategory, ...) column stores/validates the member NAME ("APP") like every
other Enum column in this codebase — the lowercase value doesn't match any defined
enum member, so loading a Plan row blew up wherever the catalogue is read.

Revision ID: c8f21a5b9d34
Revises: b6e14a08f3c2
Create Date: 2026-09-11
"""

from __future__ import annotations

from alembic import op

revision = "c8f21a5b9d34"
down_revision = "b6e14a08f3c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE plans SET category = 'APP' WHERE category = 'app'")
    op.execute("UPDATE plans SET category = 'ROUTER' WHERE category = 'router'")


def downgrade() -> None:
    op.execute("UPDATE plans SET category = 'app' WHERE category = 'APP'")
    op.execute("UPDATE plans SET category = 'router' WHERE category = 'ROUTER'")
