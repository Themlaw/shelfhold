"""Migration 0020 (M2 L01): the schema the sources of M2 need, nothing more than 0001-0016 lack."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req(
    "ENF-OPS-002",
    "EF-BIB-008", "EF-SRC-012", "EF-EXP-065", "ENF-LEG-005", "ENF-LEG-007", "EF-BIB-040",
    "EF-SRC-024", "EF-BIB-009", "EF-EXP-015", "EF-SRC-038", "EF-SRC-039", "EF-BIB-056",
    "EF-BIB-024",
)  # fmt: skip

ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
HASH = "sha256:" + "a" * 64
NOW = "2026-11-10 12:00:00.000000"


def previous_revision() -> str:
    script = ScriptDirectory(str(default_script_location()))
    down = script.get_revision("0020").down_revision
    assert isinstance(down, str)
    return down


def seed(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO user (id, username, role, can_rate) VALUES (?, 'a', 'admin', 1)", (ID,))
    db.execute(
        "INSERT INTO work (id, kind, title, sort_title) VALUES (?, 'manga', 't', 't')", (ID,)
    )
    db.execute(
        "INSERT INTO edition (id, work_id, lang, translation_type, media) "
        "VALUES (?, ?, 'fr', 'official', 'images')",
        (ID, ID),
    )
    db.execute(
        "INSERT INTO unit (id, edition_id, number_raw, number_norm) VALUES (?, ?, '1', '1')",
        (ID, ID),
    )
    db.execute(
        "INSERT INTO source_item (id, plugin_id, source_key, remote_id) "
        "VALUES (?, 'suwayomi', 'suwayomi:1', '/a')",
        (ID,),
    )
    db.execute(
        "INSERT INTO source_binding (id, work_id, edition_id, source_item_id) VALUES (?, ?, ?, ?)",
        (ID, ID, ID, ID),
    )
    db.execute(
        "INSERT INTO shelf (id, name, phrase) VALUES (?, 'Manga', 'manga')",
        (ID,),
    )
    db.execute(
        "INSERT INTO instance (instance_id, jurisdiction) VALUES (?, 'FR')",
        (ID,),
    )


@pytest.fixture
def migrated(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    db = sqlite3.connect(settings.database_path)
    db.execute("PRAGMA foreign_keys = ON")
    seed(db)
    return db


def test_upgrade_from_0019_keeps_data(settings: Settings) -> None:
    """The revision before 0020 is whatever main holds when the migration lands (0019 once M1b
    has merged its three): the test follows `down_revision` instead of a number."""
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), previous_revision())
    with closing(sqlite3.connect(database)) as db:
        seed(db)
        db.commit()

    result = migrate(settings)

    assert result.applied[:2] == ("0020", "0025")  # later ones follow
    assert {
        "metadata_field", "metadata_cache", "provider_quota", "http_cache", "import_entry",
        "plugin_attestation", "series_gap", "binding_release_sample",
    } <= tables(database)  # fmt: skip
    with closing(sqlite3.connect(database)) as db:
        assert db.execute(
            "SELECT jurisdiction, jurisdiction_confirmed_at FROM instance"
        ).fetchall() == [("FR", None)]
        assert db.execute(
            "SELECT volume_size, upstream_status, upstream_status_by, upstream_status_at "
            "FROM edition"
        ).fetchall() == [(50, "unknown", None, None)]
        assert db.execute(
            "SELECT availability, availability_detail, filtered_reason FROM unit"
        ).fetchall() == [("free", None, None)]
        assert db.execute("SELECT skip_read_upgrades, link_mode FROM shelf").fetchall() == [
            (1, "copy")
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_metadata_field_unique_per_entity_field_lang(migrated: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO metadata_field (entity_type, entity_id, field, lang, value, provider, "
        "provenance, fetched_at) VALUES ('work', ?, ?, ?, 'v', 'anilist', 'provider', ?)"
    )
    migrated.execute(insert, (ID, "synopsis", "fr", NOW))
    migrated.execute(insert, (ID, "synopsis", "en", NOW))  # another language
    migrated.execute(insert, (ID, "title", "fr", NOW))  # another field
    migrated.execute(insert, (OTHER, "synopsis", "fr", NOW))  # another entity
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(insert, (ID, "synopsis", "fr", NOW))
    row = migrated.execute(
        "SELECT locked, confidence FROM metadata_field WHERE field = 'title'"
    ).fetchone()
    assert row == (0, None)  # not locked by default
    with pytest.raises(sqlite3.IntegrityError):  # an unknown entity type
        migrated.execute(
            "INSERT INTO metadata_field (entity_type, entity_id, field, lang, value, provider, "
            "provenance, fetched_at) VALUES ('shelf', ?, 'x', 'und', 'v', 'p', 'manual', ?)",
            (ID, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):  # confidence is a ratio
        migrated.execute(
            "INSERT INTO metadata_field (entity_type, entity_id, field, lang, value, provider, "
            "provenance, confidence, fetched_at) VALUES ('work', ?, 'y', 'und', 'v', 'p', "
            "'manual', 1.5, ?)",
            (ID, NOW),
        )


def test_import_entry_state_check(migrated: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO import_entry (rel_path, size, mtime, state, first_seen_at) "
        "VALUES (?, 10, ?, ?, ?)"
    )
    for index, state in enumerate(("seen", "stored", "refused", "removed")):
        migrated.execute(insert, (f"a/{index}.cbz", NOW, state, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(insert, ("a/x.cbz", NOW, "imported", NOW))
    with pytest.raises(sqlite3.IntegrityError):  # the path is relative to the import folder
        migrated.execute(insert, ("/etc/passwd", NOW, "seen", NOW))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(insert, ("../up.cbz", NOW, "seen", NOW))
    with pytest.raises(sqlite3.IntegrityError):  # one entry per path
        migrated.execute(insert, ("a/0.cbz", NOW, "seen", NOW))
    with pytest.raises(sqlite3.IntegrityError):  # a hash says how it was computed
        migrated.execute("UPDATE import_entry SET sha256 = 'abc' WHERE rel_path = 'a/0.cbz'")
    migrated.execute("UPDATE import_entry SET sha256 = ? WHERE rel_path = 'a/0.cbz'", (HASH,))


def test_plugin_attestation_field_check(migrated: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO plugin_attestation (plugin_key, field, value, attested_by, attested_at) "
        "VALUES ('p', ?, 'v', ?, ?)"
    )
    for field in ("content_trust", "publisher", "access_partitioning", "egress"):
        migrated.execute(insert, (field, ID, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(insert, ("licence", ID, NOW))
    with pytest.raises(sqlite3.IntegrityError):  # one attestation per plugin and field
        migrated.execute(insert, ("egress", ID, NOW))
    migrated.execute("DELETE FROM user")
    assert migrated.execute("SELECT count(*) FROM plugin_attestation").fetchone() == (4,)
    assert migrated.execute("SELECT DISTINCT attested_by FROM plugin_attestation").fetchall() == [
        (None,)
    ]  # the attestation outlives the admin who made it


def test_caches_and_quota(migrated: sqlite3.Connection) -> None:
    migrated.execute(
        "INSERT INTO metadata_cache (provider, key_hash, payload, fetched_at, expires_at) "
        "VALUES ('anilist', 'k', '{}', ?, ?)",
        (NOW, NOW),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO metadata_cache (provider, key_hash, payload, fetched_at, expires_at) "
            "VALUES ('anilist', 'k', '{}', ?, ?)",
            (NOW, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):  # the payload is a JSON document
        migrated.execute(
            "INSERT INTO metadata_cache (provider, key_hash, payload, fetched_at, expires_at) "
            "VALUES ('anilist', 'j', 'not json', ?, ?)",
            (NOW, NOW),
        )
    migrated.execute(
        "INSERT INTO provider_quota (provider, tokens, refilled_at) VALUES ('anilist', 30, ?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):  # tokens never go below zero
        migrated.execute("UPDATE provider_quota SET tokens = -1")
    migrated.execute(
        "INSERT INTO http_cache (scope, url_hash, stored_at, max_age, body_ref) "
        "VALUES ('anilist', 'h', ?, 3600, 'ab/cd.bin')",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError):  # the body lives under /data/cache/http/
        migrated.execute("UPDATE http_cache SET body_ref = '../../etc/passwd'")
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("UPDATE http_cache SET max_age = -1")


def test_edition_unit_content_shelf_columns(migrated: sqlite3.Connection) -> None:
    for statement in (
        "UPDATE edition SET volume_size = 0",
        "UPDATE edition SET upstream_status = 'dead'",
        "UPDATE shelf SET link_mode = 'symlink'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(statement)
    migrated.execute("UPDATE edition SET volume_size = 30, upstream_status = 'finished'")
    migrated.execute("UPDATE shelf SET link_mode = 'hardlink', skip_read_upgrades = 0")
    migrated.execute("UPDATE unit SET availability_detail = 'early_access', filtered_reason = 'x'")
    migrated.execute("UPDATE instance SET jurisdiction_confirmed_at = ?", (NOW,))
    # the closed vocabulary of `availability` is kept: an unknown value goes in the detail
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("UPDATE unit SET availability = 'early_access'")


def test_watermark_origin_check_and_trigger_survive(migrated: sqlite3.Connection) -> None:
    migrated.execute(
        "INSERT INTO content (id, unit_id, kind, media_type, format, retention, original_sha256, "
        "store_provenance, size) VALUES (?, ?, 'images', 'application/zip', 'cbz', "
        "'adopted_ref', ?, 'extracted', 1)",
        (ID, ID, HASH),
    )
    assert migrated.execute("SELECT watermarked, watermark_origin FROM content").fetchall() == [
        (0, "none")
    ]
    for origin in ("heuristic", "user"):
        migrated.execute("UPDATE content SET watermark_origin = ?", (origin,))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("UPDATE content SET watermark_origin = 'guess'")
    triggers = {
        name
        for (name,) in migrated.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    assert {"content_coverage_cleanup", "variant_revision_no_update"} <= triggers


def test_variant_revision_quarantine_keeps_the_history_append_only(
    migrated: sqlite3.Connection,
) -> None:
    migrated.execute(
        "INSERT INTO unit_variant (id, unit_id, source_binding_id, remote_unit_id) "
        "VALUES (?, ?, ?, 'r')",
        (ID, ID, ID),
    )
    migrated.execute(
        "INSERT INTO variant_revision (variant_id, revision, seen_at) VALUES (?, 1, ?)", (ID, NOW)
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        migrated.execute("UPDATE variant_revision SET page_count = 3")
    migrated.execute(
        "UPDATE variant_revision SET quarantined_at = ?, quarantine_reason = 'suspect_shrink'",
        (NOW,),
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):  # once, never lifted
        migrated.execute("UPDATE variant_revision SET quarantine_reason = 'other'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        migrated.execute("UPDATE variant_revision SET quarantined_at = NULL")


def test_series_gap_and_release_samples(migrated: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO series_gap (edition_id, track, number_segment, number_norm, first_seen_at) "
        "VALUES (?, 'main', '', ?, ?)"
    )
    migrated.execute(insert, (ID, "12", NOW))
    with pytest.raises(sqlite3.IntegrityError):  # one row, so one notification, per gap
        migrated.execute(insert, (ID, "12", NOW))
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO series_gap (edition_id, track, number_segment, number_norm, "
            "first_seen_at) VALUES (?, 'bonus', '', '1', ?)",
            (ID, NOW),
        )
    migrated.execute("UPDATE series_gap SET notified_at = ?", (NOW,))
    migrated.execute(
        "INSERT INTO binding_release_sample (source_binding_id, observed_at) VALUES (?, ?)",
        (ID, NOW),
    )
    with pytest.raises(sqlite3.IntegrityError):  # one sample per binding and instant
        migrated.execute(
            "INSERT INTO binding_release_sample (source_binding_id, observed_at) VALUES (?, ?)",
            (ID, NOW),
        )
    migrated.execute("DELETE FROM source_binding")
    migrated.execute("DELETE FROM unit")
    migrated.execute("DELETE FROM edition")
    assert migrated.execute("SELECT count(*) FROM series_gap").fetchone() == (0,)  # cascades
    assert migrated.execute("SELECT count(*) FROM binding_release_sample").fetchone() == (0,)


def test_downgrade_removes_what_0020_added(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    config = alembic_config(database)
    command.upgrade(config, "0020")
    with closing(sqlite3.connect(database)) as db:
        seed(db)
        db.commit()
    command.downgrade(config, previous_revision())
    assert "metadata_field" not in tables(database)
    with closing(sqlite3.connect(database)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(edition)")}
        assert "volume_size" not in columns
        assert db.execute("SELECT count(*) FROM edition").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
