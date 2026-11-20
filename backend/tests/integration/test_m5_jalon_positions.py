"""Milestone M5, adverse review of the whole milestone: the integrity of the positions, across lots.

The rule that marks a Unit read (ADR-0053 as amended, `reading.domain.traversal`) was written for
KOReader, which pushes a position when the book is closed or the device sleeps: a move is plausible
when its speed, measured on `server_ts` alone, stays under `max_pages_per_minute`. The integrated
reader of M5 (L07, L10) pushes at every page, and lets its user jump anywhere (slider, table of
contents); its offline queue (L16b) keeps one position per file and sends it at reconnection. Under
the same rule, a jump to the last page made long enough after the previous position is a reading.
Each case is red today and kept as a strict xfail until the fix (contradicteur de jalon M5,
docs/decisions/M5.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import reading, storage
from shelfhold.reading.public import progress_of
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import kept, page, put


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def _paged(world: World) -> tuple[str, str]:
    """A kept 30-page volume as a scan registers it: its page count is known."""
    content, unit = kept(world)
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(storage.content).where(storage.content.c.id == content).values(page_count=30)
        )
    return content, unit


def _state(world: World, unit: str) -> str:
    admin = user_id_of(world.seed.engine, "admin")
    with world.seed.engine.connect() as connection:
        (shown,) = progress_of(connection, viewer_id=admin, subject_id=admin, unit_ids=[unit])
    return str(shown.state)


@pytest.mark.req("EF-LEC-007", "EF-LEC-014")
def test_web_reader_jump_to_last_page_an_hour_later_is_not_a_reading(world: World) -> None:
    """The reader opens a 30-page volume (page 1 is written), closes it, and an hour later jumps
    with the slider to the last page: two positions, 29 pages in 60 minutes, under 20 pages a
    minute. Not one page in between was turned: the Unit must stay `in_progress`."""
    content, unit = _paged(world)
    assert put(world, world.admin, content, page(0)).status_code == 200
    world.env.clock.advance(timedelta(minutes=60))
    assert put(world, world.admin, content, page(29)).status_code == 200
    with world.seed.engine.connect() as connection:
        marks = connection.execute(sa.select(reading.reading_position.c.read_units)).scalars().all()
    assert all(m == [] for m in marks), marks  # no position carries a mark
    assert _state(world, unit) == "in_progress"  # a look at the last page is not a reading


@pytest.mark.req("EF-LEC-007", "EF-LEC-012")
def test_offline_queue_single_position_marks_nothing_by_its_arrival(world: World) -> None:
    """The offline queue (`frontend/src/lib/offline/store.ts`, `queueNow`) keeps only the latest
    position of a file. A reader who opened a copy (page 1 written online), then looked at the last
    page offline, sends that one position when the network comes back, a day later. Its
    `server_ts` is the arrival: 29 pages in a day is « plausible », and the look is a reading."""
    content, unit = _paged(world)
    assert put(world, world.admin, content, page(0)).status_code == 200
    world.env.clock.advance(timedelta(days=1))
    late = put(world, world.admin, content, page(29), minutes=-23 * 60)  # read the day before
    assert late.status_code == 200
    assert _state(world, unit) == "in_progress"
