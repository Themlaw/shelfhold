"""Migration 0026 (M2 L12): external identifiers append-only in the database, and the journal of
attaches by external identifier (`edition_attach`), which "Séparer" reads to undo one exactly."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, normalized_ddl, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-003", "EF-BIB-055")

ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


def previous_revision() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0026").down_revision
    assert isinstance(down, str)
    return down


def test_upgrade_keeps_external_ids_and_makes_them_append_only(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), previous_revision())
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO external_id (entity_type, entity_id, provider, value, origin) "
            "VALUES ('work', ?, 'anilist', '1', 'source')",
            (ID,),
        )
        db.commit()

    assert migrate(settings).applied[-1] == "0026"

    assert "edition_attach" in tables(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT entity_id, state FROM external_id").fetchall() == [
            (ID, "current")
        ]
        for statement, params in (
            ("UPDATE external_id SET value = '2'", ()),
            ("UPDATE external_id SET entity_id = ?", (OTHER,)),
            ("DELETE FROM external_id", ()),
            (
                "INSERT OR REPLACE INTO external_id (entity_type, entity_id, provider, value, "
                "origin) VALUES ('work', ?, 'anilist', '1', 'source')",
                (OTHER,),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement, params)
        db.execute("UPDATE external_id SET state = 'superseded', origin = 'legacy'")
        assert db.execute("SELECT origin, state FROM external_id").fetchall() == [
            ("legacy", "superseded")
        ]


def test_downgrade_drops_the_journal_and_the_guards(settings: Settings) -> None:
    database = settings.database_path
    migrate(settings)
    command.downgrade(alembic_config(database), previous_revision())
    assert "edition_attach" not in tables(database)
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO external_id (entity_type, entity_id, provider, value, origin) "
            "VALUES ('work', ?, 'anilist', '1', 'source')",
            (ID,),
        )
        db.execute("DELETE FROM external_id")  # as before 0026


_SUGGESTION = (
    "INSERT INTO age_suggestion (id, entity_type, entity_id, value, x, provenance, seen_at, state)"
    " VALUES (?, ?, ?, ?, ?, ?, '2026-11-10 12:00:00.000000', ?)"
)
WORK = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
EDITION = "01ARZ3NDEKTSV4RRFFQ69G5FA2"


def _suggestions(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return list(db.execute("SELECT * FROM age_suggestion ORDER BY id"))


def _ddl(database: Path) -> dict[str, str]:
    return {k: v for k, v in normalized_ddl(database).items() if "age_suggestion" in k}


def test_age_suggestion_rebuild_keeps_rows_and_constraints(
    settings: Settings, tmp_path: Path
) -> None:
    """The verification of #134: `attach` joins the provenances; every row and every other
    constraint stays, and the downgrade gives the rows back."""
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    config = alembic_config(database)
    command.upgrade(config, previous_revision())
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO work (id, kind, title, sort_title, age_rating, age_x)"
            " VALUES (?, 'manga', 't', 't', 10, 0)",
            (WORK,),
        )
        db.execute(
            "INSERT INTO edition (id, work_id, lang, translation_type, media)"
            " VALUES (?, ?, 'en', 'official', 'images')",
            (EDITION, WORK),
        )
        for i, provenance in enumerate(("comicinfo", "opf", "sub_source", "provider", "import")):
            for j, (value, x, state) in enumerate(
                [(16, 0, "open"), (18, 1, "rejected"), (13, 0, "accepted")]
            ):
                entity = ("edition", EDITION) if j % 2 else ("work", WORK)
                db.execute(
                    _SUGGESTION,
                    (f"01ARZ3NDEKTSV4RRFFQ69G5F{i}{j}", *entity, value, x, provenance, state),
                )
        db.commit()
        before = _suggestions(db)

    command.upgrade(config, "head")
    with closing(sqlite3.connect(database)) as db:
        assert _suggestions(db) == before
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute(_SUGGESTION, ("01ARZ3NDEKTSV4RRFFQ69G5FZZ", "edition", EDITION, 16, 0,
                                 "attach", "open"))  # fmt: skip
        for refused in (
            ("01ARZ3NDEKTSV4RRFFQ69G5FZ1", "edition", EDITION, 16, 0, "nope", "open"),
            ("01ARZ3NDEKTSV4RRFFQ69G5FZ2", "edition", EDITION, 15, 0, "opf", "maybe"),
            ("01ARZ3NDEKTSV4RRFFQ69G5FZ3", "edition", EDITION, 120, 0, "opf", "open"),
            ("01ARZ3NDEKTSV4RRFFQ69G5FZ4", "shelf", EDITION, 14, 0, "opf", "open"),
            ("bad", "edition", EDITION, 12, 0, "opf", "open"),
            ("01ARZ3NDEKTSV4RRFFQ69G5FZ5", "edition", EDITION, 16, 0, "attach", "open"),
            ("01ARZ3NDEKTSV4RRFFQ69G5FZ6", "edition", EDITION, 11, None, "opf", "open"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(_SUGGESTION, refused)
        db.execute(
            "INSERT INTO age_suggestion (id, entity_type, entity_id, value, provenance, seen_at)"
            " VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FZ7', 'edition', ?, 9, 'opf',"
            " '2026-11-10 12:00:00.000000')",
            (EDITION,),
        )
        defaults = db.execute(
            "SELECT x, state FROM age_suggestion WHERE id = '01ARZ3NDEKTSV4RRFFQ69G5FZ7'"
        ).fetchone()
        assert defaults == (0, "open")
        db.execute("DELETE FROM age_suggestion WHERE id = '01ARZ3NDEKTSV4RRFFQ69G5FZ7'")
        db.commit()
    fresh = tmp_path / "fresh.db"
    command.upgrade(alembic_config(fresh), "head")
    assert _ddl(database) == _ddl(fresh)

    command.downgrade(config, previous_revision())
    with closing(sqlite3.connect(database)) as db:
        assert _suggestions(db) == before  # the `attach` row goes, the others stay
