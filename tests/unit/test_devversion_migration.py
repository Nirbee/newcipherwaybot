"""DevVersion reports migration (revision a6d2f8c4e1b9), run on a throwaway sqlite: the table with
its indexes, the cascade from subscriptions, and the downgrade."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[2] / "src/infrastructure/database/migrations/versions"
_MIGRATION = _VERSIONS / "a6d2f8c4e1b9_devversion_reports.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("devversion_reports", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_and_downgrade() -> None:
    migration = _load()
    assert migration.down_revision == "e8b3c5a1f9d2"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        conn.exec_driver_sql("CREATE TABLE subscriptions (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO subscriptions (id) VALUES (1)")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        insp = sa.inspect(conn)
        assert {i["name"] for i in insp.get_indexes("devversion_reports")} == {
            "ix_devversion_reports_subscription_id",
            "ix_devversion_reports_received_at",
            "ix_devversion_reports_asn",
        }
        conn.exec_driver_sql(
            "INSERT INTO devversion_reports (subscription_id, install_id, app_version, "
            "consent_version, schema, asn, size, payload, digest) "
            "VALUES (1, 'id', '0.5.0', '1', 1, 4200000000, 2, '{}', '{}')"
        )
        row = conn.exec_driver_sql("SELECT received_at, asn, size FROM devversion_reports").one()
        assert row[0] is not None and row[1] == 4200000000  # server default; 32-bit unsigned ASN
        assert row[2] == 2
        conn.exec_driver_sql("DELETE FROM subscriptions WHERE id = 1")
        assert conn.exec_driver_sql("SELECT count(*) FROM devversion_reports").scalar() == 0
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "devversion_reports" not in sa.inspect(conn).get_table_names()
