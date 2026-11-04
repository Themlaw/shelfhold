"""Migration 0003 (catalog and acquisition): single head, parity, upgrade from 0002, downgrade.

The DDL parity of the whole database is `test_migrated_ddl_equals_create_all` (0002 module): it
runs at head, so it covers this revision's CHECK constraints, partial indexes and triggers too.
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
from shelfhold.infrastructure.schema import acquisition, catalog, metadata
from tests.integration.test_schema_migration_0002 import PLATFORM_TABLES, alembic_config, tables

CATALOG_TABLES = {
    "work_kind", "series", "series_member", "work", "work_title", "contributor",
    "contributor_name", "contribution", "external_id", "edition", "volume", "unit",
    "unit_variant", "variant_revision", "team", "team_alias", "team_priority",
    "merge_suggestion", "age_suggestion", "normalizer", "work_relation", "work_relation_kind",
}  # fmt: skip
ACQUISITION_TABLES = {
    "source_item", "sub_source", "source_alias", "source_binding", "series_follow", "gap_ack",
    "download_request", "download_request_unit", "source_failure", "source_config",
    "source_account", "upstream_snapshot", "plugin_install", "plugin_state", "revision_pending",
}  # fmt: skip


@pytest.mark.req("ENF-OPS-002")
def test_single_alembic_head() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0003"]
    chain = [rev.revision for rev in script.walk_revisions()]
    assert chain == ["0003", "0002", "0001"]  # linear, newest first


@pytest.mark.req("ENF-OPS-002", "EF-BIB-001")
def test_metadata_covers_exactly_the_migrated_tables(tmp_path: Path) -> None:
    database = tmp_path / "all.db"
    command.upgrade(alembic_config(database), "head")
    expected = PLATFORM_TABLES | CATALOG_TABLES | ACQUISITION_TABLES
    assert tables(database) == {t.name for t in metadata.sorted_tables} == expected
    assert {t.name for t in vars(catalog).values() if isinstance(t, sa.Table)} == CATALOG_TABLES
    assert {
        t.name for t in vars(acquisition).values() if isinstance(t, sa.Table)
    } == ACQUISITION_TABLES


@pytest.mark.req("ENF-OPS-002")
def test_upgrade_from_0002_keeps_existing_rows(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0002")
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO app_setting (key, value) VALUES ('log_level', 'DEBUG')")
        db.execute(
            "INSERT INTO user (id, username, role, can_rate, locale, timezone, title_lang_pref, "
            "color_scheme) VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAV', 'admin', 'admin', 1, 'fr', "
            "'UTC', '[]', 'auto')"
        )
        db.commit()
    assert current_revision(database) == "0002"

    result = migrate(settings)

    assert result.applied == ("0003",)
    assert result.backup is not None
    assert "0002-to-0003" in result.backup.name
    assert current_revision(database) == "0003"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT username FROM user").fetchall() == [("admin",)]
        assert db.execute("SELECT value FROM app_setting").fetchall() == [("DEBUG",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert tables(database) >= CATALOG_TABLES | ACQUISITION_TABLES


@pytest.mark.req("ENF-OPS-002")
def test_downgrade_to_0002_removes_exactly_the_new_objects(tmp_path: Path) -> None:
    database = tmp_path / "down.db"
    config = alembic_config(database)
    command.upgrade(config, "0002")
    with closing(sqlite3.connect(database)) as db:
        before = db.execute("SELECT type, name FROM sqlite_master ORDER BY 1, 2").fetchall()
    command.upgrade(config, "head")
    command.downgrade(config, "0002")
    with closing(sqlite3.connect(database)) as db:
        after = db.execute("SELECT type, name FROM sqlite_master ORDER BY 1, 2").fetchall()
    assert after == before
    command.upgrade(config, "head")  # and back up again
    assert tables(database) >= CATALOG_TABLES


@pytest.mark.req("EF-BIB-001")
def test_work_kinds_are_seeded_by_the_migration(tmp_path: Path) -> None:
    database = tmp_path / "seed.db"
    command.upgrade(alembic_config(database), "head")
    with closing(sqlite3.connect(database)) as db:
        rows = dict(db.execute("SELECT key, default_media FROM work_kind").fetchall())
    assert set(rows) >= {
        "manga",
        "manhwa",
        "manhua",
        "webtoon",
        "comic",
        "bd",
        "light_novel",
        "novel",
        "ebook",
    }
    assert rows["novel"] == "text"
    assert rows["manga"] == "images"
    assert "audio" not in rows  # audio is a medium of the Edition, not a kind (EF-BIB-001)


@pytest.mark.req("ENF-OPS-002")
def test_new_triggers_are_in_the_migrated_database(tmp_path: Path) -> None:
    database = tmp_path / "triggers.db"
    command.upgrade(alembic_config(database), "head")
    with closing(sqlite3.connect(database)) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {
        "variant_revision_no_update",
        "variant_revision_no_replace",
        "variant_revision_no_delete",
    } <= names


@pytest.mark.req("EF-BIB-039")
def test_work_relation_kinds_are_seeded_by_the_migration(tmp_path: Path) -> None:
    database = tmp_path / "relations.db"
    command.upgrade(alembic_config(database), "head")
    with closing(sqlite3.connect(database)) as db:
        keys = {r[0] for r in db.execute("SELECT key FROM work_relation_kind")}
    assert keys == {"adaptation_of", "sequel_of", "prequel_of", "spin_off_of"}
