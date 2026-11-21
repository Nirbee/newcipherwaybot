"""LAN Play rooms migration (revision e8b3c5a1f9d2), run on a throwaway sqlite.

The suite builds its schema with create_all (no migrations), so the DDL is exercised here:
both tables appear with their unique constraints and indexes, and the downgrade drops them."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[2] / "src/infrastructure/database/migrations/versions"
_MIGRATION = _VERSIONS / "e8b3c5a1f9d2_lan_rooms.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lan_rooms", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_and_downgrade() -> None:
    migration = _load()
    assert migration.down_revision == "d4f8a2c6e1b3"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE subscriptions (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO subscriptions (id) VALUES (1), (2)")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        insp = sa.inspect(conn)
        assert {"lan_rooms", "lan_room_members", "lan_accounts", "lan_room_bans"} <= set(
            insp.get_table_names()
        )
        assert {i["name"] for i in insp.get_indexes("lan_room_members")} == {
            "ix_lan_room_members_subscription_id"
        }
        conn.exec_driver_sql(
            "INSERT INTO lan_rooms (id, code, name, owner_subscription_id) "
            "VALUES (1, 'ABC234', 'Комната', 1)"
        )
        conn.exec_driver_sql(
            "INSERT INTO lan_room_members (room_id, subscription_id) VALUES (1, 1), (1, 2)"
        )
        conn.exec_driver_sql(
            "INSERT INTO lan_accounts (subscription_id, hs_user) VALUES (1, 'lan-00000000a1')"
        )
        conn.exec_driver_sql("INSERT INTO lan_room_bans (room_id, subscription_id) VALUES (1, 2)")
        joined = conn.exec_driver_sql("SELECT joined_at FROM lan_room_members").all()
        assert all(row[0] is not None for row in joined)  # server default
    for sql in (
        "INSERT INTO lan_rooms (code, name, owner_subscription_id) VALUES ('ABC234', 'x', 2)",
        "INSERT INTO lan_room_members (room_id, subscription_id) VALUES (1, 2)",
        "INSERT INTO lan_accounts (subscription_id, hs_user) VALUES (1, 'lan-00000000b2')",
        "INSERT INTO lan_accounts (subscription_id, hs_user) VALUES (2, 'lan-00000000a1')",
        "INSERT INTO lan_room_bans (room_id, subscription_id) VALUES (1, 2)",
    ):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.exec_driver_sql(sql)  # duplicate code / duplicate (room, subscription)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        left = set(sa.inspect(conn).get_table_names())
        assert not {"lan_rooms", "lan_room_members", "lan_accounts", "lan_room_bans"} & left


def test_single_head() -> None:
    """The chain has exactly one head (the LAN revision didn't fork it)."""
    parents: dict[str, str | None] = {}
    for path in _VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?:: \w+)? = "(\w+)"', text, re.M)
        down = re.search(r'^down_revision(?:: [^=]+)? = "(\w+)"', text, re.M)
        assert rev is not None, path.name
        parents[rev.group(1)] = down.group(1) if down else None
    heads = set(parents) - set(parents.values())
    assert len(heads) == 1, heads
    assert parents["e8b3c5a1f9d2"] == "d4f8a2c6e1b3"
