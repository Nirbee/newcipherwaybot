"""subscriptions.panel_username — the panel user's name (service accounts, «Клиенты» list)

Revision ID: b7e3d9a2c5f1
Revises: a6d2f8c4e1b9
Create Date: 2026-11-23
"""

from __future__ import annotations

import fnmatch
import json

import sqlalchemy as sa
from alembic import op

revision = "b7e3d9a2c5f1"
down_revision = "a6d2f8c4e1b9"
branch_labels = None
depends_on = None

# The default of PANEL_SERVICE_USERNAMES (MultiScript's cascade relay and status monitor).
_SERVICE_PATTERNS = ("cascade_relay_b74178", "ms-status-monitor", "cascade_relay_*")


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("panel_username", sa.String(length=64)))
    # Backfill only what protection needs right after the deploy: the panel import named a
    # person without Telegram after their panel username (users.first_name), so MultiScript's
    # service accounts adopted on 6 Aug are recognisable here before the panel is ever asked.
    # Everyone else is filled from the panel snapshot of the «Клиенты» list.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT s.id, s.plan_snapshot, u.first_name FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE u.telegram_id IS NULL AND u.first_name IS NOT NULL"
        )
    ).all()
    for sub_id, snapshot, first_name in rows:
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except ValueError:
                snapshot = None
        if not isinstance(snapshot, dict) or snapshot.get("source") != "panel":
            continue
        name = str(first_name).strip()
        if not any(fnmatch.fnmatchcase(name.lower(), p) for p in _SERVICE_PATTERNS):
            continue
        conn.execute(
            sa.text("UPDATE subscriptions SET panel_username = :name WHERE id = :id"),
            {"name": name[:64], "id": sub_id},
        )


def downgrade() -> None:
    op.drop_column("subscriptions", "panel_username")
