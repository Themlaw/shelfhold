"""Normalizer versions of the identity keys (ENF-OPS-021) and migration 0012 (PLAN L46).

The upgrade test builds a database as release 0011 left it, then migrates it for real; the others
go through the production writers (`Catalog.ensure_edition`, the follow cycle).
"""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from shelfhold.catalog.public import Catalog, EditionSpec
from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import current_revision, migrate
from tests.adopt_env import adoption, comicinfo
from tests.catalog_env import harness
from tests.export_env import ExportEnv, export_env
from tests.follow_env import follow_bench
from tests.integration.test_schema_migration_0002 import alembic_config
from tests.ops_env import make_ops

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
            "INSERT INTO edition (id, work_id, lang, lang_raw, translation_type, media) "
            "VALUES (?, ?, 'fr', 'fr', 'official', 'images')",
            (ID.format("V"), ID.format("V")),
        )
        db.execute(  # an adopted Edition: no raw form, and one without a language
            "INSERT INTO edition (id, work_id, lang, translation_type, media) "
            "VALUES (?, ?, 'x-unknown', 'official', 'images')",
            (ID.format("W"), ID.format("V")),
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
        assert db.execute("SELECT id, lang, lang_norm_by FROM edition ORDER BY id").fetchall() == [
            (ID.format("V"), "fr", "lang@1"),
            (ID.format("W"), "x-unknown", None),
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
        edition = h.edition(lang="FR-fr")
        row = h.connection.exec_driver_sql(
            "SELECT lang, lang_raw, lang_norm_by FROM edition WHERE id = ?", (edition.id,)
        ).one()
        assert row[1] == "FR-fr"  # as received
        assert row[0] != "FR-fr"  # normalized
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


# -- review of PR #73: every path that writes an Edition language says which normalizer ran ------


def _editions(engine: sa.Engine) -> dict[str, tuple[str, str | None, str | None]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT id, lang, lang_raw, lang_norm_by FROM edition ORDER BY id")
        )
        return {r.id: (r.lang, r.lang_raw, r.lang_norm_by) for r in rows}


def _adopted_and_relabelled(env: ExportEnv) -> dict[str, str]:
    """Adoption of two series (one with a language written oddly, one without), then the admin
    gives the second one a language. Returns the Edition ids by series title."""
    a = adoption(env)
    a.add_series("Dr Stone", ["Dr Stone - Ch.1.cbz"], info=comicinfo("Dr Stone", language="FR-fr"))
    a.add_series("Monster", ["Monster - Ch.1.cbz"], info=comicinfo("Monster", language=""))
    a.adopt()
    with env.engine.connect() as connection:
        ids = {
            r.title: r.id
            for r in connection.execute(
                sa.text("SELECT w.title, e.id FROM edition e JOIN work w ON w.id = e.work_id")
            )
            if r.title in ("Dr Stone", "Monster")
        }
    return ids


@pytest.mark.req("ENF-OPS-021")
def test_edition_lang_norm_by_written_by_adoption() -> None:
    with export_env() as env:
        ids = _adopted_and_relabelled(env)
        editions = _editions(env.engine)
        lang, raw, by = editions[ids["Dr Stone"]]
        assert raw == "FR-fr"  # as Kavita gave it
        assert lang != "FR-fr" and by == "lang@1"  # normalized, and says so
        assert editions[ids["Monster"]] == ("x-unknown", None, None)  # the placeholder is no value


@pytest.mark.req("ENF-OPS-021")
def test_edition_lang_norm_by_null_for_discovery_without_language() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.server.state["mangas"][2]["inLibrary"] = True  # source 7003: `all`, no language
        assert bench.run().unknown_language == 1
        rows = bench.rows("SELECT lang, lang_norm_by FROM edition WHERE lang = 'x-unknown'")
        assert [(r["lang"], r["lang_norm_by"]) for r in rows] == [("x-unknown", None)]
        known = bench.rows("SELECT lang_norm_by FROM edition WHERE lang <> 'x-unknown'")
        assert {r["lang_norm_by"] for r in known} >= {"lang@1"}


@pytest.mark.req("ENF-OPS-021")
def test_edition_lang_norm_by_written_by_set_lang() -> None:
    with export_env() as env:
        ids = _adopted_and_relabelled(env)
        assert make_ops(env).edition_set_lang(ids["Monster"], "ja") == "ja"
        assert _editions(env.engine)[ids["Monster"]] == ("ja", "ja", "lang@1")


@pytest.mark.req("ENF-OPS-021", "ENF-OPS-002")
def test_migrated_database_matches_a_fresh_one_for_lang_norm_by(tmp_path: Path) -> None:
    """Editions written by every production path, then the file is taken back to 0011 and
    migrated again: the migration lands on what the code wrote."""
    with export_env() as env:
        ids = _adopted_and_relabelled(env)
        make_ops(env).edition_set_lang(ids["Monster"], "ko")
        with env.engine.begin() as connection:
            books = Catalog(connection)
            books.ensure_edition(
                EditionSpec(books.create_work("manga", "W"), "En-us", "fan", "images")
            )
        fresh = _editions(env.engine)
        database = tmp_path / "again.db"
        env.engine.dispose()
        shutil.copyfile(str(env.engine.url.database), database)
    assert {by for _, _, by in fresh.values()} >= {None, "lang@1"}  # both kinds are there
    config = alembic_config(database)
    command.downgrade(config, "0011")
    command.upgrade(config, "head")
    engine = create_db_engine(database)
    try:
        assert _editions(engine) == fresh
    finally:
        engine.dispose()
