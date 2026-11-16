"""Backfill of ticket_messages.source (revision d4f8a2c6e1b3), run on a throwaway sqlite.

The suite builds its schema with create_all (no migrations), so the upgrade's raw SQL is
exercised here directly: customer messages take their thread's channel (APP -> app,
CABINET -> mini-app, BOT -> bot), staff/AI messages stay NULL."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src/infrastructure/database/migrations/versions/d4f8a2c6e1b3_ticket_message_source.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_message_source", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_maps_channels_and_skips_staff_messages() -> None:
    migration = _load()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE tickets (id INTEGER PRIMARY KEY, channel VARCHAR(16))")
        conn.exec_driver_sql(
            "CREATE TABLE ticket_messages (id INTEGER PRIMARY KEY, ticket_id INTEGER, "
            "author VARCHAR(8))"
        )
        conn.exec_driver_sql(
            "INSERT INTO tickets (id, channel) VALUES (1, 'BOT'), (2, 'CABINET'), (3, 'APP')"
        )
        conn.exec_driver_sql(
            "INSERT INTO ticket_messages (id, ticket_id, author) VALUES "
            "(1, 1, 'USER'), (2, 1, 'ADMIN'), (3, 2, 'USER'), (4, 3, 'USER'), (5, 3, 'ADMIN')"
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = conn.exec_driver_sql("SELECT id, source FROM ticket_messages ORDER BY id").all()
    assert [tuple(r) for r in rows] == [
        (1, "BOT"),
        (2, None),
        (3, "MINIAPP"),
        (4, "APP"),
        (5, None),
    ]


def test_backfilled_names_are_ticket_source_members() -> None:
    """The column stores the member NAME (SQLAlchemy Enum convention) — the backfill must
    write names the model can load."""
    from src.core.enums import TicketSource

    assert {"BOT", "MINIAPP", "APP"} <= {s.name for s in TicketSource}
