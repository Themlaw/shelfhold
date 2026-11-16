"""M4 L15: changing the preferred Edition carries each profile's reading over (EF-BIB-026,
EF-BIB-044), and nobody else's.

The position a profile had in the old Edition becomes a neutral one (`work_id`, `part_ordinal`,
`totalProgression`, `completed`, `approximate = true`) and is written in the new one as a
`transposed` position: it counts in `reading_progress`, always `approximate`, and it is never a
proof of reading: no anchor of a traversal, nothing pushed to Kavita.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from shelfhold.reading.public import prefer_edition, record_position, undo_read_mark
from tests.integration.test_schema_catalog import add_edition
from tests.reading_env import NEW_HASH, ReadingEnv, reading_env
from tests.visibility_env import RATED, _work

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
FR_HASH = "sha256:" + "c3" * 32
pytestmark = pytest.mark.req("EF-BIB-026", "EF-BIB-044")


def bilingual(env: ReadingEnv, *, fr_age: int | None = None) -> tuple[str, str, str]:
    """A Work with an English Edition (the preferred one) and a French one: (work, en, fr)."""
    with env.engine.begin() as connection:
        kind = connection.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one()
        work = _work(connection, str(kind), "Tale of Two Tongues", age=6)
        en = add_edition(connection, work, lang="en", age_rating=6, rated_at=RATED, preferred=True)
        fr = add_edition(
            connection, work, lang="fr", age_rating=fr_age or 6, rated_at=RATED, preferred=False
        )
    env.seed.works["en"], env.seed.editions["en"] = work, en
    env.seed.works["fr"], env.seed.editions["fr"] = work, fr
    return work, en, fr


def chapters(
    env: ReadingEnv, key: str, count: int, *, hashed: str | None = None
) -> tuple[list[str], list[str]]:
    """`count` chapters, one file each. Returns (unit ids, file ids)."""
    units = env.units(key, count)
    files = [env.file(key, [u], content_hash=hashed) for u in units]
    return units, files


def flags(env: ReadingEnv, work: str) -> dict[str, bool]:
    with env.engine.connect() as connection:
        rows = connection.execute(
            sa.select(catalog.edition.c.lang, catalog.edition.c.preferred).where(
                catalog.edition.c.work_id == work
            )
        )
        return {str(r.lang): bool(r.preferred) for r in rows}


def change(env: ReadingEnv, edition: str) -> Any:
    with env.engine.begin() as connection:
        return prefer_edition(connection, edition_id=edition, now=NOW)


def transposed(env: ReadingEnv, user: str) -> list[Any]:
    with env.engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT * FROM reading_position WHERE user_id = :u "
                "AND json_extract(locator, '$.transposed') = 1 ORDER BY id"
            ),
            {"u": user},
        )
        return [SimpleNamespace(**{**r._mapping, "locator": json.loads(r.locator)}) for r in rows]


def test_preferred_edition_change_transfers_neutral_position_approximate(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        work, _en, fr = bilingual(env)
        en_units, en_files = chapters(env, "en", 3, hashed=NEW_HASH)
        fr_units, _ = chapters(env, "fr", 3, hashed=FR_HASH)
        env.position(reader, en_files[2], percent=0.5)  # chapter 3, half way

        done = change(env, fr)

        assert flags(env, work) == {"en": False, "fr": True}
        assert done.transferred == 1 and done.pending == 0
        progress = env.progress(reader)
        assert progress[fr_units[2]].state == "in_progress"
        assert bool(progress[fr_units[2]].approximate) is True  # always: a guess, not a reading
        assert fr_units[0] not in progress and fr_units[1] not in progress  # no chapter is skipped
        (row,) = transposed(env, reader)
        assert row.percent == pytest.approx(0.5)
        assert row.locator["neutral"] == {
            "work_id": work, "part_ordinal": 3, "total_progression": pytest.approx(2.5 / 3),
            "completed": False, "approximate": True, "part_number": 3.0,
            "part_progression": 0.5,
        }  # fmt: skip
        assert row.device_id == "transfer" and row.source == "device"
        assert env.progress(reader)[en_units[2]].state == "in_progress"  # the old one is kept


def test_what_the_old_edition_marked_read_stays_read_in_the_new_one_and_can_be_undone(
    tmp_path: Path,
) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work_id, _en, fr = bilingual(env)
        _en_units, en_files = chapters(env, "en", 3, hashed=NEW_HASH)
        fr_units, _ = chapters(env, "fr", 3, hashed=FR_HASH)
        env.position(reader, en_files[0], percent=1.0, source="server")
        env.position(reader, en_files[1], percent=1.0, source="server")
        env.position(reader, en_files[2], percent=0.4)

        change(env, fr)

        progress = env.progress(reader)
        assert [progress[u].state for u in fr_units] == ["read", "read", "in_progress"]
        assert all(bool(progress[u].approximate) for u in fr_units)
        with env.engine.begin() as connection:
            assert (
                undo_read_mark(connection, viewer_id=reader, subject_id=reader, unit_id=fr_units[0])
                >= 1
            )
        assert env.progress(reader)[fr_units[0]].state != "read"
        assert env.progress(reader)[fr_units[1]].state == "read"


def test_volume_page_transposed_to_chapter_by_coverage_bounds(tmp_path: Path) -> None:
    """Page 143 of an English volume of chapters 1 to 5 is the French chapter that covers it."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work_id, _en, fr = bilingual(env)
        en_units = env.units("en", 5)
        bounds = [(1, 40), (41, 80), (81, 120), (121, 160), (161, 200)]
        volume = env.file("en", en_units, bounds=bounds, content_hash=NEW_HASH)
        fr_units, _ = chapters(env, "fr", 5, hashed=FR_HASH)
        env.position(reader, volume, page=143, percent=143 / 200)

        change(env, fr)

        progress = env.progress(reader)
        assert progress[fr_units[3]].state == "in_progress"  # 143 is in chapter 4 (121 to 160)
        assert bool(progress[fr_units[3]].approximate) is True
        assert fr_units[4] not in progress and fr_units[0] not in progress  # skipped, never marked
        (row,) = transposed(env, reader)
        assert row.percent == pytest.approx(22 / 39)  # where 143 sits in 121..160
        assert row.locator["neutral"]["part_ordinal"] == 4


def test_a_chapter_position_lands_in_the_volume_that_covers_it(tmp_path: Path) -> None:
    """The other way: English chapters, a French volume of 4 chapters. No page is invented."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work_id, _en, fr = bilingual(env)
        _en_units, en_files = chapters(env, "en", 4, hashed=NEW_HASH)
        fr_units = env.units("fr", 4)
        env.file("fr", fr_units, bounds=[(1, 10), (11, 20), (21, 30), (31, 40)],
                 content_hash=FR_HASH)  # fmt: skip
        env.position(reader, en_files[1], percent=0.5)

        change(env, fr)

        progress = env.progress(reader)
        assert progress[fr_units[1]].state == "in_progress"
        assert bool(progress[fr_units[1]].approximate) is True
        assert progress[fr_units[2]].state == "unread"  # the next chapter is not reached
        (row,) = transposed(env, reader)
        assert "page" not in row.locator and row.percent == pytest.approx((1 + 0.5) / 4)


def test_a_work_read_to_the_end_arrives_completed(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work_id, _en, fr = bilingual(env)
        _eu, en_files = chapters(env, "en", 2, hashed=NEW_HASH)
        fr_units, _ = chapters(env, "fr", 2, hashed=FR_HASH)
        for item in en_files:
            env.position(reader, item, percent=1.0, source="server")

        change(env, fr)

        assert [env.progress(reader)[u].state for u in fr_units] == ["read", "read"]
        assert transposed(env, reader)[-1].locator["neutral"]["completed"] is True


def test_the_change_is_applied_once_and_a_second_call_adds_nothing(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work_id, _en, fr = bilingual(env)
        _eu, en_files = chapters(env, "en", 2, hashed=NEW_HASH)
        chapters(env, "fr", 2, hashed=FR_HASH)
        env.position(reader, en_files[1], percent=0.5)
        change(env, fr)
        again = change(env, fr)
        assert len(transposed(env, reader)) == 1 and again.transferred == 0


def test_a_profile_with_no_reading_gets_nothing_and_nobody_gets_anothers(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader, idle = env.user("reader"), env.user("idle")
        admin = env.user("boss", "admin")
        _work_id, _en, fr = bilingual(env)
        _eu, en_files = chapters(env, "en", 2, hashed=NEW_HASH)
        fr_units, _ = chapters(env, "fr", 2, hashed=FR_HASH)
        env.position(reader, en_files[1], percent=0.5)

        change(env, fr)

        assert env.progress(idle) == {} and env.progress(admin) == {}  # IDOR: nothing leaks over
        assert transposed(env, idle) == [] and transposed(env, admin) == []
        assert set(env.progress(reader)) >= {fr_units[1]}


def test_a_child_gets_nothing_in_an_edition_above_her_ceiling(tmp_path: Path) -> None:
    """The French Edition is rated 18: the child (ceiling 10) reads on, in English, and the
    transfer neither writes in it nor makes it appear in her progress."""
    with reading_env(tmp_path) as env:
        kid, adult = env.user("kid", "child"), env.user("adult")
        _work_id, _en, fr = bilingual(env, fr_age=18)
        en_units, en_files = chapters(env, "en", 2, hashed=NEW_HASH)
        fr_units, _ = chapters(env, "fr", 2, hashed=FR_HASH)
        env.position(kid, en_files[1], percent=0.5)
        env.position(adult, en_files[1], percent=0.5)

        change(env, fr)

        assert transposed(env, kid) == []
        assert set(env.progress(kid)) == {en_units[1]}
        assert len(transposed(env, adult)) == 1 and fr_units[1] in env.progress(adult)


def test_a_child_whose_old_edition_is_above_her_ceiling_carries_nothing_over(
    tmp_path: Path,
) -> None:
    with reading_env(tmp_path) as env:
        kid = env.user("kid", "child")
        _work_id, _en, fr = bilingual(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == env.seed.editions["en"])
                .values(age_rating=18)
            )
        _eu, en_files = chapters(env, "en", 2, hashed=NEW_HASH)
        chapters(env, "fr", 2, hashed=FR_HASH)
        env.position(kid, en_files[1], percent=0.5)  # left by an earlier state of the ceiling

        change(env, fr)

        assert transposed(env, kid) == []


def test_an_edition_with_no_file_yet_keeps_the_progress_pending_not_invented(
    tmp_path: Path,
) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work_id, _en, fr = bilingual(env)
        _eu, en_files = chapters(env, "en", 2, hashed=NEW_HASH)
        env.units("fr", 2)  # known, not downloaded
        env.position(reader, en_files[1], percent=0.5)

        done = change(env, fr)

        assert done.transferred == 0 and done.pending == 1
        assert transposed(env, reader) == []


@pytest.mark.parametrize("which", ["unknown", "other-work"])
def test_only_an_edition_of_the_work_can_become_preferred(tmp_path: Path, which: str) -> None:
    with reading_env(tmp_path) as env:
        _work_id, en, _fr = bilingual(env)
        target = new_ulid() if which == "unknown" else env.seed.editions["twelve"]
        with env.engine.begin() as connection:
            before = connection.execute(sa.select(catalog.edition.c.preferred)).all()
        if which == "unknown":
            with pytest.raises(LookupError):
                change(env, target)
        else:
            change(env, target)  # alone in its Work: preferred, and nothing else moves
        with env.engine.connect() as connection:
            assert flags(env, env.seed.works["en"]) == {"en": True, "fr": False}
            if which == "unknown":
                assert connection.execute(sa.select(catalog.edition.c.preferred)).all() == before
        assert en


def test_at_most_one_edition_of_a_work_is_preferred(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        work, en, fr = bilingual(env)
        change(env, fr)
        change(env, en)
        assert flags(env, work) == {"en": True, "fr": False}


# -- the contracts the relays hold ---------------------------------------------------------------
def test_a_transposed_position_is_never_the_anchor_of_a_traversal(tmp_path: Path) -> None:
    """A move from the transposed position to the end would be plausible (10 h for the file);
    from nothing it proves nothing: the chapter is not read."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work_id, _en, fr = bilingual(env)
        _eu, en_files = chapters(env, "en", 1, hashed=NEW_HASH)
        fr_units, fr_files = chapters(env, "fr", 1, hashed=FR_HASH)
        env.position(reader, en_files[0], percent=0.02)  # 2 pages of 100: a start
        change(env, fr)
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE content SET page_count = 100 WHERE content_hash = :h"),
                {"h": FR_HASH},
            )
            record_position(
                connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
                carrier_id=fr_files[0], content_hash=FR_HASH, locator={}, percent=1.0,
                client_ts=NOW + timedelta(hours=10), now=NOW + timedelta(hours=10),
            )  # fmt: skip
        assert env.progress(reader)[fr_units[0]].state == "in_progress"


def test_the_pages_given_to_kavita_never_come_from_a_transposed_position(tmp_path: Path) -> None:
    """`reading.application.sync` reads the device positions since the last pass: a transposed
    one, however recent, is neither a page to push nor a mark of reading."""
    from shelfhold.reading.application import sync

    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work_id, _en, fr = bilingual(env)
        _eu, en_files = chapters(env, "en", 2, hashed=NEW_HASH)
        fr_units, fr_files = chapters(env, "fr", 2, hashed=FR_HASH)
        env.position(reader, en_files[0], percent=1.0, source="server")
        env.position(reader, en_files[1], percent=0.5)
        change(env, fr)  # fr chapter 1 is read (a mark), chapter 2 half way
        refs = [
            sync.ChapterRef(
                chapter_id=n, volume_id=n, series_id=1, library_id=1, pages=10, carrier_id=item
            )
            for n, item in enumerate(fr_files, start=1)
        ]
        assert sync._pending_chapters(env.engine, reader, refs, {}) == set()
        outcome = sync.MemberOutcome("ok")
        for ref in refs:
            assert sync._pending_page(env.engine, reader, ref, sync.EPOCH, outcome) is None
        assert fr_units


def test_the_view_and_the_domain_agree_on_a_transposed_position(tmp_path: Path) -> None:
    """A single-chapter file is never `approximate` from a share alone; a transposed one is."""
    from shelfhold.reading.domain.projection import Bounds, project

    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        plain, other = env.file("ok", units[:1]), env.file("ok", units[1:])
        env.position(reader, plain, percent=0.5)
        row = env.position(reader, other, percent=0.5)
        with env.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE reading_position SET locator = '{\"transposed\": true}' WHERE id = :i"
                ),
                {"i": row},
            )
        got = env.progress(reader)
        for unit, flag in ((units[0], False), (units[1], True)):
            want = project(
                page=None, percent=0.5, bounds=Bounds(None, None, 0, 1), trusted=False,
                transposed=flag,
            )  # fmt: skip
            assert bool(got[unit].approximate) is want.approximate is flag
