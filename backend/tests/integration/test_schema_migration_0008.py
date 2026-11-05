"""Migration 0008: `export_op.op = 'register'` (L28, `pull` plans, bug found in L27)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from shelfhold.infrastructure.schema import coherence, metadata
from tests.integration.test_schema_migration_0002 import alembic_config, normalized_ddl


def export_op_sql(database: Path) -> str:
    with closing(sqlite3.connect(database)) as db:
        row = db.execute("SELECT sql FROM sqlite_master WHERE name = 'export_op'").fetchone()
    return str(row[0])


@pytest.mark.req("EF-EXP-062")
def test_upgrade_allows_register_and_downgrade_removes_it(tmp_path: Path) -> None:
    database = tmp_path / "a.db"
    config = alembic_config(database)
    command.upgrade(config, "0007")
    assert "'register'" not in export_op_sql(database)
    command.upgrade(config, "0008")
    assert "'register'" in export_op_sql(database)
    command.downgrade(config, "0007")
    assert "'register'" not in export_op_sql(database)


@pytest.mark.req("EF-EXP-062")
def test_the_check_constraint_matches_the_metadata(tmp_path: Path) -> None:
    """`test_migrated_ddl_equals_create_all` compares everything; this one names the table."""
    migrated, created = tmp_path / "m.db", tmp_path / "c.db"
    command.upgrade(alembic_config(migrated), "head")
    engine = sa.create_engine(f"sqlite:///{created}")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    assert "register" in coherence.PLAN_OPS
    assert normalized_ddl(migrated)["table:export_op"] == normalized_ddl(created)["table:export_op"]
