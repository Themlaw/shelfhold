"""Migration 0009: `tombstone_lost_kid`, the kids of the "lost keyring" waiver (L33)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from shelfhold.infrastructure.schema import metadata
from tests.integration.test_schema_migration_0002 import alembic_config, normalized_ddl, tables

pytestmark = pytest.mark.req("EF-BIB-054", "ENF-OPS-002")


def test_upgrade_creates_and_downgrade_drops_the_lost_kid_table(tmp_path: Path) -> None:
    database = tmp_path / "a.db"
    config = alembic_config(database)
    command.upgrade(config, "0008")
    assert "tombstone_lost_kid" not in tables(database)
    command.upgrade(config, "0009")
    assert "tombstone_lost_kid" in tables(database)
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO tombstone_kid (kid, first_seq) VALUES ('k-1', 1)")
        db.execute("INSERT INTO tombstone_lost_kid (kid) VALUES ('k-1')")
        db.commit()
        for statement in (
            "DELETE FROM tombstone_lost_kid",
            "UPDATE tombstone_lost_kid SET actor_id = 'x'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement)
    command.downgrade(config, "0008")
    assert "tombstone_lost_kid" not in tables(database)


def test_lost_kid_ddl_matches_the_metadata(tmp_path: Path) -> None:
    """`test_migrated_ddl_equals_create_all` compares everything; this one names the objects."""
    migrated, created = tmp_path / "m.db", tmp_path / "c.db"
    command.upgrade(alembic_config(migrated), "head")
    engine = sa.create_engine(f"sqlite:///{created}")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    left, right = normalized_ddl(migrated), normalized_ddl(created)
    for key in (
        "table:tombstone_lost_kid",
        "trigger:tombstone_lost_kid_no_update",
        "trigger:tombstone_lost_kid_no_delete",
    ):
        assert left[key] == right[key], key
