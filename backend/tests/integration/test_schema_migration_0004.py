"""Migration 0004 (storage, export, reading): single head, parity, upgrade from 0003, downgrade.

The DDL parity of the whole database is `test_migrated_ddl_equals_create_all` (0002 module): it
runs at head, so it covers this revision's CHECK constraints, partial indexes, triggers and view.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, default_script_location, migrate
from shelfhold.infrastructure.schema import export, metadata, reading, storage
from tests.integration.test_schema_migration_0002 import (
    PLATFORM_TABLES,
    alembic_config,
    normalized_ddl,
    tables,
)
from tests.integration.test_schema_migration_0003 import ACQUISITION_TABLES, CATALOG_TABLES

STORAGE_TABLES = {"content", "trash_item", "replica"}
EXPORT_TABLES = {
    "export_target", "kavita_instance", "shelf", "shelf_destination", "remote_library",
    "shelf_destination_remote_library", "export_item", "unit_coverage", "export_record",
    "export_series", "edition_export_series", "field_owner", "kavita_access_audit",
    "document_alias",
}  # fmt: skip
READING_TABLES = {"reading_position"}
M1B_TABLES = {"webauthn_credential", "backup_run"}  # migration 0013
ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


@pytest.mark.req("ENF-OPS-002")
def test_single_alembic_head() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0015"]
    chain = [rev.revision for rev in script.walk_revisions()]
    assert chain == [
        "0015",
        "0014",
        "0013",
        "0012",
        "0011",
        "0010",
        "0009",
        "0008",
        "0007",
        "0006",
        "0005",
        "0004",
        "0003",
        "0002",
        "0001",
    ]  # linear, newest first


@pytest.mark.req("ENF-OPS-002", "EF-BIB-045", "EF-EXP-058")
def test_metadata_covers_exactly_the_migrated_tables(tmp_path: Path) -> None:
    database = tmp_path / "all.db"
    command.upgrade(alembic_config(database), "head")
    expected = (
        PLATFORM_TABLES
        | CATALOG_TABLES
        | ACQUISITION_TABLES
        | STORAGE_TABLES
        | EXPORT_TABLES
        | READING_TABLES
        | M1B_TABLES
    )
    assert tables(database) == {t.name for t in metadata.sorted_tables} == expected
    for module, names in ((storage, STORAGE_TABLES), (export, EXPORT_TABLES)):
        assert {t.name for t in vars(module).values() if isinstance(t, sa.Table)} == names
    assert {t.name for t in vars(reading).values() if isinstance(t, sa.Table)} == READING_TABLES


@pytest.mark.req("ENF-OPS-002")
def test_upgrade_from_0003_keeps_existing_rows_and_fills_the_new_foreign_keys(
    settings: Settings,
) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0003")
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO app_setting (key, value) VALUES ('log_level', 'DEBUG')")
        db.execute(
            "INSERT INTO work (id, kind, title, sort_title) VALUES (?, 'manga', 't', 't')",
            (ULID,),
        )
        db.execute(
            "INSERT INTO edition (id, work_id, lang, translation_type, media) "
            "VALUES (?, ?, 'fr', 'official', 'images')",
            (ULID, ULID),
        )
        db.execute(
            "INSERT INTO export_plan (plan_id, shelf_destination_id, kind, dry_run, base_version) "
            "VALUES (?, NULL, 'export', 0, 0)",
            (ULID,),
        )
        db.commit()
    assert current_revision(database) == "0003"

    result = migrate(settings)

    assert result.applied == (
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
        "0010",
        "0011",
        "0012",
        "0013",
        "0014",
        "0015",
    )
    assert result.backup is not None
    assert "0003-to-0015" in result.backup.name
    assert current_revision(database) == "0015"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT value FROM app_setting").fetchall() == [("DEBUG",)]
        assert db.execute("SELECT id, shelf_id FROM edition").fetchall() == [(ULID, None)]
        assert db.execute("SELECT plan_id FROM export_plan").fetchall() == [(ULID,)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert tables(database) >= STORAGE_TABLES | EXPORT_TABLES | READING_TABLES


@pytest.mark.req("EF-EXP-042", "EF-EXP-058")
def test_export_plan_destination_is_a_real_foreign_key_after_0004(tmp_path: Path) -> None:
    database = tmp_path / "plan.db"
    command.upgrade(alembic_config(database), "head")
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        insert = (
            "INSERT INTO export_plan (plan_id, shelf_destination_id, kind, dry_run, base_version) "
            "VALUES (?, ?, 'export', 0, 0)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(insert, (ULID, OTHER))
        db.execute(insert, (ULID, None))  # a plan without a destination (global) stays possible
        db.execute(
            "INSERT INTO export_op (id, plan_id, batch, position, op) VALUES (?, ?, 0, 0, 'mkdir')",
            (ULID, ULID),
        )
        db.execute(
            "INSERT INTO fs_intent (id, op, intent_schema_version, plan_id, state, epoch) "
            "VALUES (?, 'rename', '1', ?, 'pending', 0)",
            (ULID, ULID),
        )
        db.commit()  # the children of export_plan survive the rebuild of the table


@pytest.mark.req("ENF-OPS-002", "EF-BIB-050")
def test_rebuilding_unit_variant_keeps_the_append_only_guard(tmp_path: Path) -> None:
    database = tmp_path / "guard.db"
    command.upgrade(alembic_config(database), "head")
    with closing(sqlite3.connect(database)) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {
        "variant_revision_no_update",
        "variant_revision_no_replace",
        "variant_revision_no_delete",
    } <= names


@pytest.mark.req("ENF-OPS-002")
def test_downgrade_to_0003_removes_exactly_the_new_objects(tmp_path: Path) -> None:
    database = tmp_path / "down.db"
    config = alembic_config(database)
    command.upgrade(config, "0003")
    before = normalized_ddl(database)
    command.upgrade(config, "head")
    command.downgrade(config, "0003")
    after = normalized_ddl(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert after == before  # tables, indexes, triggers and the former export_plan, unchanged
    command.upgrade(config, "head")  # and back up again
    assert tables(database) >= STORAGE_TABLES


@pytest.mark.req("EF-LEC-014")
def test_reading_progress_view_is_in_the_migrated_database(tmp_path: Path) -> None:
    database = tmp_path / "view.db"
    config = alembic_config(database)
    command.upgrade(config, "head")
    with closing(sqlite3.connect(database)) as db:
        kinds = dict(db.execute("SELECT name, type FROM sqlite_master WHERE name LIKE 'reading_%'"))
        assert kinds["reading_progress"] == "view"
        assert db.execute("SELECT * FROM reading_progress").fetchall() == []
    command.downgrade(config, "0003")
    with closing(sqlite3.connect(database)) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'reading_%'").fetchall()


@pytest.mark.req("EF-BIB-045", "EF-EXP-043")
def test_new_triggers_are_in_the_migrated_database(tmp_path: Path) -> None:
    database = tmp_path / "triggers.db"
    command.upgrade(alembic_config(database), "head")
    with closing(sqlite3.connect(database)) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {
        "export_item_coverage_cleanup",
        "content_coverage_cleanup",
    } <= names


@pytest.mark.req("EF-EXP-060", "EF-EXP-059")
def test_physical_root_has_a_mode_rw_or_adopt_ro(tmp_path: Path) -> None:
    database = tmp_path / "mode.db"
    config = alembic_config(database)
    command.upgrade(config, "0003")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO physical_root (root_id, path_entry) VALUES (?, '/library/a')", (ULID,)
        )
        db.commit()
    command.upgrade(config, "head")
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT mode FROM physical_root").fetchall() == [("rw",)]  # the default
        db.execute(
            "INSERT INTO physical_root (root_id, path_entry, mode) "
            "VALUES (?, '/library/b', 'adopt_ro')",
            (OTHER,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="ck_physical_root_mode_values"):
            db.execute(
                "INSERT INTO physical_root (root_id, path_entry, mode) "
                "VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAX', '/library/c', 'rw+')"
            )


@pytest.mark.req("ENF-OPS-002")
def test_filled_0003_database_migrates_then_downgrades_without_losing_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "round.db"
    config = alembic_config(database)
    command.upgrade(config, "0003")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO work (id, kind, title, sort_title) VALUES (?, 'manga', 't', 't')", (ULID,)
        )
        db.execute(
            "INSERT INTO edition (id, work_id, lang, translation_type, media) "
            "VALUES (?, ?, 'fr', 'official', 'images')",
            (ULID, ULID),
        )
        db.execute(
            "INSERT INTO unit (id, edition_id, number_raw, number_norm) VALUES (?, ?, '1', '1')",
            (ULID, ULID),
        )
        db.commit()
    command.upgrade(config, "head")
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT shelf_id FROM edition").fetchall() == [(None,)]
    command.downgrade(config, "0003")
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT id FROM edition").fetchall() == [(ULID,)]
        assert db.execute("SELECT id FROM unit").fetchall() == [(ULID,)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
