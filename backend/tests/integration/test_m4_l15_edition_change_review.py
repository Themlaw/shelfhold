"""#306 (M4 L15), adversarial review: proofs of concept, each red (`xfail`, strict) until fixed.

A transposed position is a guess. None of what follows may turn it into a "read" that nobody made,
inside Shelfhold or pushed outside (Suwayomi L17, Kavita L14b/L50), nor bring back a mark the
member undid. See `docs/decisions/M4.md`, section "#306 : contradicteur".
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.reading.public import prefer_edition, undo_read_mark
from tests.integration.test_m4_l15_edition_change import (
    FR_HASH,
    NOW,
    bilingual,
    change,
    chapters,
)
from tests.integration.test_schema_catalog import add_edition
from tests.read_state_env import _HASH, read_state_env
from tests.reading_env import NEW_HASH, reading_env
from tests.visibility_env import RATED

pytestmark = pytest.mark.req("EF-BIB-026", "EF-BIB-044")


def test_a_chapter_number_never_marks_read_the_volume_of_the_same_number(tmp_path: Path) -> None:
    """English in chapters (1 to 10), French in volumes (1 to 3, EPUB `book` Units, as an import
    of volume files makes them). Chapters 1 to 3 read, chapter 4 half way: volumes 2 and 3 cover
    chapters 4 to 10, which nobody read; the number 2 of a chapter is not the number 2 of a
    volume."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work, _en, fr = bilingual(env)
        _en_units, en_files = chapters(env, "en", 10, hashed=NEW_HASH)
        fr_units, _ = chapters(env, "fr", 3, hashed=FR_HASH)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.unit).where(catalog.unit.c.id.in_(fr_units)).values(kind="book")
            )
        for item in en_files[:3]:
            env.position(reader, item, percent=1.0, source="server")
        env.position(reader, en_files[3], percent=0.5)

        change(env, fr)

        progress = env.progress(reader)
        assert progress.get(fr_units[1]) is None or progress[fr_units[1]].state != "read"
        assert progress.get(fr_units[2]) is None or progress[fr_units[2]].state != "read"


def test_a_transposed_mark_is_never_pushed_read_to_suwayomi(tmp_path: Path) -> None:
    """L17 pushes what the view says read, "a plausible traversal, or a state a server holds, and
    nothing else". A mark carried over from another Edition is neither."""
    with read_state_env(tmp_path) as env:
        admin = env.reading.users["root"]  # the owner by default
        seed = env.reading.seed
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == seed.editions["ok"])
                .values(preferred=False)
            )
            en = add_edition(
                connection, seed.works["ok"], lang="de", age_rating=6, rated_at=RATED,
                preferred=True,
            )  # fmt: skip
        seed.works["en"], seed.editions["en"] = seed.works["ok"], en
        (en_unit,) = env.reading.units("en", 1, first=101)  # the twin of the chapter below
        en_file = env.reading.file("en", [en_unit], content_hash=NEW_HASH)
        env.reading.position(admin, en_file, percent=1.0, source="server")  # read in English
        chapter = env.chapter()  # number 101 in the Suwayomi Edition
        with write_transaction(env.engine) as connection:
            content = env.reading._content(connection, "ok", chapter.unit, _HASH)
            connection.execute(
                sa.update(export.export_item)
                .where(export.export_item.c.id == chapter.item)
                .values(content_id=content)
            )
        with write_transaction(env.engine) as connection:
            done = prefer_edition(connection, edition_id=seed.editions["ok"], now=NOW)
        assert done.transferred == 1
        assert env.state(admin, chapter) == "read"  # the transposed mark, approximate

        env.push()

        assert not env.is_read_there(chapter)
        assert env.fake.read_updates == []


def test_a_look_at_the_last_page_never_becomes_read_in_kavita_through_a_transposed_mark(
    tmp_path: Path,
) -> None:
    """`push_page` gives Kavita its "read" (`page == pages`) when `_all_read` says every Unit of the
    file is read. A transposed mark makes it say so: a mere look at page 9 of 10 of the French
    chapter is pushed as read, which L14b reserves to a traversal and L15 says never happens."""
    from shelfhold.reading.application import sync

    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work, _en, fr = bilingual(env)
        _eu, en_files = chapters(env, "en", 1, hashed=NEW_HASH)
        _fu, fr_files = chapters(env, "fr", 1, hashed=FR_HASH)
        env.position(reader, en_files[0], percent=1.0, source="server")
        change(env, fr)
        later = NOW + timedelta(minutes=5)
        env.position(
            reader, fr_files[0], page=9, percent=0.9, content_hash=FR_HASH, client_ts=later,
            server_ts=later, device_id="reader-1",
        )  # fmt: skip
        ref = sync.ChapterRef(
            chapter_id=1, volume_id=1, series_id=1, library_id=1, pages=10, carrier_id=fr_files[0]
        )

        wanted = sync._pending_page(env.engine, reader, ref, sync.EPOCH, sync.MemberOutcome("ok"))

        assert wanted != 10  # 10 is Kavita's "read"; a look is at most `pages - 2`


def test_a_chapter_half_covered_by_a_read_volume_is_not_carried_over_read(tmp_path: Path) -> None:
    """The English volume holds chapter 1 and the first half of chapter 2 (`extent = partial`);
    Kavita says the volume read. The second half of chapter 2 was never read: the French chapter 2
    is not a "read" (it would be pushed to Suwayomi, B2)."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work, _en, fr = bilingual(env)
        en_units = env.units("en", 2)
        volume = env.file("en", en_units, bounds=[(1, 30), (31, 40)], content_hash=NEW_HASH)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.unit_coverage)
                .where(
                    export.unit_coverage.c.carrier_id == volume,
                    export.unit_coverage.c.unit_id == en_units[1],
                )
                .values(extent="partial")
            )
        fr_units, _ = chapters(env, "fr", 2, hashed=FR_HASH)
        env.position(reader, volume, page=40, percent=1.0, source="server")

        change(env, fr)

        assert env.progress(reader)[fr_units[1]].state != "read"


def test_a_mark_the_member_undid_does_not_come_back_with_a_second_call(tmp_path: Path) -> None:
    """The member undoes the transposed mark of French chapter 1, then reads on in English (the old
    Edition stays readable); the administrator calls the door again (the documented way out of
    `pending`). The new neutral position differs, so a new position is written, and its
    `read_units` mark chapter 1 again: the undo is lost."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _work, _en, fr = bilingual(env)
        _eu, en_files = chapters(env, "en", 2, hashed=NEW_HASH)
        fr_units, _ = chapters(env, "fr", 2, hashed=FR_HASH)
        env.position(reader, en_files[0], percent=1.0, source="server")
        env.position(reader, en_files[1], percent=0.5)
        change(env, fr)
        with env.engine.begin() as connection:
            assert (
                undo_read_mark(connection, viewer_id=reader, subject_id=reader, unit_id=fr_units[0])
                >= 1
            )
        assert env.progress(reader)[fr_units[0]].state != "read"
        later = NOW + timedelta(hours=1)
        env.position(reader, en_files[1], percent=0.8, client_ts=later, server_ts=later)

        change(env, fr)

        assert env.progress(reader)[fr_units[0]].state != "read"
