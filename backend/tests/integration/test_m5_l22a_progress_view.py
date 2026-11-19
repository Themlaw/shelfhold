"""M5 L22a (EF-LEC-014, debt of M4 #306): `extent` is read by the progress view.

A file that covers a Unit only in part (`unit_coverage.extent = 'partial'`) says nothing of the
rest of that Unit. A server that says the file read, or a plausible walk through its pages, makes
the Unit at most `in_progress`; only a file that covers it whole (or another file that does) reads
it. The view is the one criterion of every consumer (revisions, upgrades, ranged export, pushes to
Suwayomi and Kavita): a partial Unit that arrives `read` there is read for all of them.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export
from shelfhold.reading.public import record_position
from tests.reading_env import NEW_HASH, T0, ReadingEnv, reading_env

pytestmark = pytest.mark.req("EF-LEC-014")


def _partial(env: ReadingEnv, carrier: str, unit: str) -> None:
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.update(export.unit_coverage)
            .where(
                export.unit_coverage.c.carrier_id == carrier,
                export.unit_coverage.c.unit_id == unit,
            )
            .values(extent="partial")
        )


def _states(env: ReadingEnv, user: str, view: str) -> dict[str, str]:
    with env.engine.connect() as connection:
        rows = connection.execute(
            sa.text(f"SELECT unit_id, state FROM {view} WHERE user_id = :u"), {"u": user}  # noqa: S608
        )
        return {str(r.unit_id): str(r.state) for r in rows}


def test_partial_extent_counted_in_progress_view(tmp_path: Path) -> None:
    """The volume holds chapter 1 and the first half of chapter 2; the server says it read. The
    view says chapter 1 read and chapter 2 in progress, in both views (`reading_progress` and
    `reading_progress_proven`, the one that decides what leaves for Suwayomi and Kavita)."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        volume = env.file("ok", units, bounds=[(1, 30), (31, 40)], content_hash=NEW_HASH)
        _partial(env, volume, units[1])
        env.position(reader, volume, page=40, percent=1.0, source="server")

        for view in ("reading_progress", "reading_progress_proven"):
            assert _states(env, reader, view) == {units[0]: "read", units[1]: "in_progress"}, view


def test_a_unit_covered_whole_by_another_file_is_still_read(tmp_path: Path) -> None:
    """`extent` is a property of a file's cover, not of the Unit: the same chapter 2, whole in its
    own file, is read when that file is."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        volume = env.file("ok", units, bounds=[(1, 30), (31, 40)], content_hash=NEW_HASH)
        _partial(env, volume, units[1])
        alone = env.file("ok", [units[1]], bounds=[(1, 20)], content_hash="sha256:" + "d4" * 32)
        env.position(reader, volume, page=40, percent=1.0, source="server")
        env.position(reader, alone, page=20, percent=1.0, source="server")

        assert _states(env, reader, "reading_progress")[units[1]] == "read"


def test_a_walk_through_the_pages_of_a_partial_cover_marks_nothing_read(tmp_path: Path) -> None:
    """A plausible traversal of the pages a file holds of a Unit is not a reading of the Unit: the
    walk crosses chapter 1 (whole) and the first half of chapter 2, and only the first is read."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        volume = env.file("ok", units, bounds=[(1, 30), (31, 40)], content_hash=NEW_HASH)
        _partial(env, volume, units[1])
        for page, later in ((1, T0), (40, T0 + timedelta(hours=2))):
            with write_transaction(env.engine) as connection:
                record_position(
                    connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
                    carrier_id=volume, content_hash=NEW_HASH, locator={"page": page},
                    percent=None, client_ts=later, now=later,
                )  # fmt: skip

        assert _states(env, reader, "reading_progress") == {
            units[0]: "read",
            units[1]: "in_progress",
        }
