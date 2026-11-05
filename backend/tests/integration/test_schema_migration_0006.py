"""Migration 0006 (`fs_intent.op = 'write'`, hex `export_plan.base_version`)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from tests.integration.test_schema_migration_0002 import alembic_config

PLAN = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
INTENT = (
    "INSERT INTO fs_intent (id, op, intent_schema_version, state, epoch) "
    "VALUES (?, ?, '1', 'pending', 1)"
)
PLAN_SQL = (
    "INSERT INTO export_plan (plan_id, kind, dry_run, base_version) VALUES (?, 'export', 1, ?)"
)


@pytest.mark.req("EF-EXP-041", "EF-EXP-042")
def test_upgrade_keeps_plans_and_opens_the_write_op_and_hex_versions(tmp_path: Path) -> None:
    database = tmp_path / "a.db"
    config = alembic_config(database)
    command.upgrade(config, "0005")
    with closing(sqlite3.connect(database)) as db:
        db.execute(PLAN_SQL, (PLAN, 7))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(INTENT, (OTHER, "write"))
        db.commit()

    command.upgrade(config, "0006")

    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT plan_id, base_version FROM export_plan").fetchall() == [
            (PLAN, "7")
        ]
        db.execute(INTENT, (OTHER, "write"))
        db.execute(PLAN_SQL, (OTHER, "0123456789abcdef0123456789abcdef"))
        for bad in ("XYZ", "-1", "", "A" * 8):
            with pytest.raises(sqlite3.IntegrityError, match="base_version"):
                db.execute(PLAN_SQL, (OTHER[:-1] + "X", bad))
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.req("EF-EXP-041")
def test_downgrade_drops_what_the_old_schema_cannot_hold(tmp_path: Path) -> None:
    database = tmp_path / "d.db"
    config = alembic_config(database)
    command.upgrade(config, "0006")
    with closing(sqlite3.connect(database)) as db:
        db.execute(PLAN_SQL, (PLAN, "12"))
        db.execute(PLAN_SQL, (OTHER, "0123456789abcdef0123456789abcdef"))
        db.execute(INTENT, (OTHER, "write"))
        db.commit()

    command.downgrade(config, "0005")

    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT plan_id, base_version FROM export_plan").fetchall() == [
            (PLAN, 12)
        ]
        assert db.execute("SELECT count(*) FROM fs_intent").fetchone() == (0,)
