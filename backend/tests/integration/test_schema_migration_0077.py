"""Migration 0077 (M4 L49a): the block of a series (ADR-0053 §3, §5, §8; ADR-0048).

Three tables. `bundle_partition`: where a part begins, written when it is made and never moved
(an `export_item` of kind `bundle` carries the aliases of the part). `bundle_version`: one file a
profile can download, counted per (Edition, part, profile). `bundle_member`: what a version is
made of, with a keyed fingerprint (`content_fp`) and never a clear hash; it is wiped when the
file's row is purged. The DDL parity of the whole database is `test_migrated_ddl_equals_create_all`.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-007")

FP = "ab" * 32
_COLUMNS = (
    "INSERT INTO bundle_member (version_id, rank, export_item_id, chapter_label, content_fp, "
    "pages_digest, first_page, page_count, page_map_state) VALUES "
)
MEMBER_NO_ITEM = _COLUMNS + "(?, 1, NULL, '1', ?, ?, 1, 1, 'candidate')"
MEMBER_WITH_ITEM = _COLUMNS + "(?, 1, ?, '1', ?, ?, 1, 1, 'candidate')"


@pytest.mark.req("ENF-OPS-002")
def test_single_alembic_head_with_0077_on_0065() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0077"]
    assert script.get_revision("0077").down_revision == "0065"


@pytest.mark.req("ENF-OPS-002")
def test_upgrade_from_0065_keeps_data_and_adds_the_tables(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0065")
    assert "bundle_version" not in tables(database)
    assert "0077" in migrate(settings).applied
    assert {"bundle_partition", "bundle_version", "bundle_member"} <= set(tables(database))


def seed(conn: sqlite3.Connection) -> dict[str, str]:
    ids = {k: new_ulid() for k in ("work", "edition", "user", "item", "unit", "version")}
    conn.execute("PRAGMA foreign_keys = ON")
    kind = conn.execute("SELECT key FROM work_kind LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO work (id, kind, title, sort_title) VALUES (?, ?, 't', 't')",
        (ids["work"], kind),
    )
    conn.execute(
        "INSERT INTO edition (id, work_id, lang, translation_type, media) "
        "VALUES (?, ?, 'en', 'official', 'images')",
        (ids["edition"], ids["work"]),
    )
    conn.execute(
        'INSERT INTO "user" (id, username, role, created_at) '
        "VALUES (?, 'reader', 'member', '2026-11-01 10:00:00')",
        (ids["user"],),
    )
    conn.execute(
        "INSERT INTO export_item (id, edition_id, kind) VALUES (?, ?, 'bundle')",
        (ids["item"], ids["edition"]),
    )
    conn.execute(
        "INSERT INTO bundle_partition (edition_id, part_index, first_unit_id, export_item_id) "
        "VALUES (?, 1, NULL, ?)",
        (ids["edition"], ids["item"]),
    )
    return ids


def version(conn: sqlite3.Connection, ids: dict[str, str], **override: object) -> str:
    row: dict[str, object] = {
        "id": new_ulid(), "edition_id": ids["edition"], "part_index": 1,
        "profile_user_id": ids["user"], "number": 1, "content_fp": FP,
        "koreader_md5": "md5:" + "0" * 32, "state_key": FP, "size": 10, "entry_count": 1,
        "page_count": 1, "member_count": 1, "excluded": "{}",
    }  # fmt: skip
    row.update(override)
    conn.execute(
        f"INSERT INTO bundle_version ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
        tuple(row.values()),
    )
    return str(row["id"])


def test_a_number_is_counted_once_per_edition_part_and_profile_and_a_fingerprint_is_hex(
    settings: Settings,
) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        ids = seed(conn)
        version(conn, ids)
        with pytest.raises(sqlite3.IntegrityError):
            version(conn, ids)  # same number for the same (Edition, part, profile)
        version(conn, ids, number=2)
        with pytest.raises(sqlite3.IntegrityError):
            version(conn, ids, number=3, content_fp="not-hex")
        with pytest.raises(sqlite3.IntegrityError):
            version(conn, ids, number=0)
        with pytest.raises(sqlite3.IntegrityError):
            version(conn, ids, number=4, part_index=2)  # no such part


def test_deleting_the_profile_or_the_edition_deletes_the_versions_and_their_members(
    settings: Settings,
) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        ids = seed(conn)
        made = version(conn, ids)
        conn.execute(
            MEMBER_NO_ITEM,
            (made, FP, FP),
        )
        conn.execute('DELETE FROM "user" WHERE id = ?', (ids["user"],))
        assert conn.execute("SELECT count(*) FROM bundle_version").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM bundle_member").fetchone() == (0,)


def test_a_purged_file_wipes_its_fingerprints_and_the_member_stays_as_an_orphan(
    settings: Settings,
) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        ids = seed(conn)
        made = version(conn, ids)
        conn.execute(
            "INSERT INTO export_item (id, edition_id, kind) VALUES (?, ?, 'unit')",
            (ids["unit"], ids["edition"]),
        )
        conn.execute(
            MEMBER_WITH_ITEM,
            (made, ids["unit"], FP, FP),
        )
        conn.execute("DELETE FROM export_item WHERE id = ?", (ids["unit"],))
        assert conn.execute(
            "SELECT export_item_id, content_fp, pages_digest FROM bundle_member"
        ).fetchall() == [(None, None, None)]


def test_a_member_with_an_unknown_page_state_is_refused(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        ids = seed(conn)
        made = version(conn, ids)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO bundle_member (version_id, rank, chapter_label, first_page, "
                "page_count, page_map_state) VALUES (?, 1, '1', 1, 1, 'verified-by-magic')",
                (made,),
            )


@pytest.mark.req("ENF-OPS-002")
def test_downgrade_to_0065_drops_the_tables(settings: Settings) -> None:
    migrate(settings)
    command.downgrade(alembic_config(settings.database_path), "0065")
    assert "bundle_version" not in tables(settings.database_path)
    assert "bundle_member" not in tables(settings.database_path)
    assert "bundle_partition" not in tables(settings.database_path)
