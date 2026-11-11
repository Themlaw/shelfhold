"""Migration 0025 (cross-cutting review 2026-11-11): the Edition age suggestions that rating a Work
rejected are open again when nobody rated the Edition and they are above the Work's rating."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from tests.integration.test_schema_migration_0002 import alembic_config

WORK = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
UNRATED = "01ARZ3NDEKTSV4RRFFQ69G5FA2"  # an Edition nobody rated on its own
RATED = "01ARZ3NDEKTSV4RRFFQ69G5FA3"  # an Edition `rate_edition` ruled on
SOURCE = "01ARZ3NDEKTSV4RRFFQ69G5FA4"


@pytest.mark.req("EF-BIB-055", "ENF-OPS-002")
def test_rejected_edition_suggestions_above_the_work_are_reopened(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0020")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO work (id, kind, title, sort_title, age_rating, age_x)"
            " VALUES (?, 'manga', 't', 't', 10, 0)",
            (WORK,),
        )
        for edition, age in ((UNRATED, None), (RATED, 10)):
            db.execute(
                "INSERT INTO edition (id, work_id, lang, translation_type, media,"
                " age_rating, age_x)"
                " VALUES (?, ?, ?, 'official', 'images', ?, 0)",
                (edition, WORK, "en" if edition == UNRATED else "fr", age),
            )
        rows = [
            ("S1", UNRATED, 18, 1, "rejected"),  # rejected by the old rate(work): reopened
            ("S2", UNRATED, 10, 0, "rejected"),  # idem, whatever the age of the Work
            ("S3", RATED, 18, 1, "rejected"),  # the Edition was rated: a decision, stays
            ("S4", UNRATED, 16, 0, "accepted"),  # accepted by the old rate(work): reopened
        ]
        for i, (_name, entity, value, x, state) in enumerate(rows):
            db.execute(
                "INSERT INTO age_suggestion (id, entity_type, entity_id, value, x, provenance,"
                " seen_at, state)"
                " VALUES (?, 'edition', ?, ?, ?, 'sub_source', '2026-11-10 12:00:00.000000', ?)",
                (f"01ARZ3NDEKTSV4RRFFQ69G5F{i}{SOURCE[-1]}", entity, value, x, state),
            )
        db.commit()

    assert migrate(settings).applied[:1] == ("0025",)  # later ones follow

    with closing(sqlite3.connect(database)) as db:
        got = [r[0] for r in db.execute("SELECT state FROM age_suggestion ORDER BY id")]
    assert got == ["open", "open", "rejected", "open"]  # S1 to S4: S3 is the rated Edition
