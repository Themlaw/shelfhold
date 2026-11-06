"""Normalizer versions of the identity keys (ENF-OPS-021) and migration 0012 (PLAN L46).

The upgrade test builds a database as release 0011 left it, then migrates it for real; the others
go through the production writers (`Catalog.ensure_edition`, the follow cycle).
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, migrate
from tests.catalog_env import harness
from tests.follow_env import follow_bench
from tests.integration.test_schema_migration_0002 import alembic_config

ID = "01ARZ3NDEKTSV4RRFFQ69G5FA{}"


@pytest.mark.req("ENF-OPS-021", "ENF-OPS-002")
def test_upgrade_from_0011_fills_norm_by_and_keeps_data(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0011")
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute(
            "INSERT INTO work (id, kind, title, sort_title) VALUES (?, 'manga', 't', 't')",
            (ID.format("V"),),
        )
        db.execute(
            "INSERT INTO edition (id, work_id, lang, translation_type, media) "
            "VALUES (?, ?, 'fr', 'official', 'images')",
            (ID.format("V"), ID.format("V")),
        )
        db.execute(
            "INSERT INTO source_item (id, plugin_id, source_key, remote_id) "
            "VALUES (?, 'suwayomi', 'k', 'r')",
            (ID.format("V"),),
        )
        db.execute(
            "INSERT INTO source_binding "
            "(id, work_id, edition_id, source_item_id, priority, active) "
            "VALUES (?, ?, ?, ?, 1, 1)",
            (ID.format("V"),) * 4,
        )
        db.execute(
            "INSERT INTO unit (id, edition_id, track, number_segment, number_raw, number_norm, "
            "part, source_order) VALUES (?, ?, 'main', '', '1', '1', 1, 1)",
            (ID.format("V"), ID.format("V")),
        )
        db.execute(
            "INSERT INTO unit_variant (id, unit_id, source_binding_id, remote_unit_id) "
            "VALUES (?, ?, ?, 'ch-1')",
            (ID.format("V"),) * 3,
        )
        db.commit()
    assert current_revision(database) == "0011"

    result = migrate(settings)

    assert result.applied[0] == "0012"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT id, lang, lang_norm_by FROM edition").fetchall() == [
            (ID.format("V"), "fr", "lang@1")
        ]
        assert db.execute(
            "SELECT remote_unit_id, remote_unit_id_norm_by FROM unit_variant"
        ).fetchall() == [("ch-1", "remote_unit_id@1")]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = {r[1] for r in db.execute("PRAGMA table_info(export_record)")}
        assert "resolved_at" in columns


@pytest.mark.req("ENF-OPS-021")
def test_edition_lang_norm_by_written_by_ensure_edition() -> None:
    with harness() as h:
        edition = h.edition(lang="FR_fr")
        row = h.connection.exec_driver_sql(
            "SELECT lang, lang_raw, lang_norm_by FROM edition WHERE id = ?", (edition.id,)
        ).one()
        assert row[1] == "FR_fr"  # as received
        assert row[0] != "FR_fr"  # normalized
        assert row[2] == "lang@1"


@pytest.mark.req("ENF-OPS-021", "EF-SRC-036")
def test_variant_remote_unit_id_norm_by_written_by_the_follow_cycle() -> None:
    with follow_bench() as bench:
        bench.run()
        rows = bench.rows(
            "SELECT remote_unit_id, remote_unit_id_norm_by FROM unit_variant "
            "WHERE source_binding_id = :b",
            b=bench.binding_id,
        )
        assert len(rows) == 2  # what the cycle recorded for the followed series
        assert {r["remote_unit_id_norm_by"] for r in rows} == {"remote_unit_id@1"}
