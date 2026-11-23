"""subscriptions.panel_username (revision b7e3d9a2c5f1), run on a throwaway sqlite.

The suite builds its schema with create_all (no migrations), so the backfill is exercised here:
MultiScript's service accounts adopted by the panel import (named after their panel username,
no Telegram) are recognised right after the deploy; nobody else is touched."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src/infrastructure/database/migrations/versions/b7e3d9a2c5f1_subscription_panel_username.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("subscription_panel_username", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_marks_only_adopted_service_accounts() -> None:
    migration = _load()
    assert migration.down_revision == "a6d2f8c4e1b9"
    engine = sa.create_engine("sqlite://")
    panel = '{"name": "Из панели", "source": "panel"}'
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT, "
            "first_name VARCHAR(128))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "plan_snapshot JSON)"
        )
        conn.exec_driver_sql(
            "INSERT INTO users (id, telegram_id, first_name) VALUES "
            "(94, NULL, 'cascade_relay_b74178'), (98, NULL, 'ms-status-monitor'), "
            "(5, NULL, 'NL-017'), (6, 5072755730, 'ms-status-monitor'), "
            "(7, NULL, 'ms-hand-made')"
        )
        conn.execute(
            sa.text("INSERT INTO subscriptions (id, user_id, plan_snapshot) VALUES (:i, :u, :s)"),
            [
                {"i": 1, "u": 94, "s": panel},
                {"i": 2, "u": 98, "s": panel},
                {"i": 3, "u": 5, "s": panel},  # an ordinary adopted customer
                {"i": 4, "u": 6, "s": panel},  # has Telegram — named by Telegram, not the panel
                {"i": 5, "u": 7, "s": '{"name": "Тариф"}'},  # bought in the bot
            ],
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = conn.exec_driver_sql("SELECT id, panel_username FROM subscriptions ORDER BY id")
        assert [tuple(r) for r in rows.all()] == [
            (1, "cascade_relay_b74178"),
            (2, "ms-status-monitor"),
            (3, None),
            (4, None),
            (5, None),
        ]
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "panel_username" not in {
            c["name"] for c in sa.inspect(conn).get_columns("subscriptions")
        }
