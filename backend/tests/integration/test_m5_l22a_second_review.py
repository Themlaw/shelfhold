"""The second independent review of M5 L22a (#386): what the first adverse review did not see.

Three holes, one proof each, red until they are fixed (`xfail(strict=True)`):

* **A walk through a series block marks read a Unit that its file covers only in part.** 0106
  says a Unit a file covers in part is at most `in_progress` from that file, "whatever walk crossed
  its pages", and the write side was closed in `progress._mark_traversed`. The other writer of
  walk marks, `mark_traversed_in_frame` (kosync on the block of a series, M4 L47), was not
  touched: it marks every member's Unit it crosses, partial or not. The view does not catch it
  either: its filter looks at the coverage of the position's own carrier (one member file), while
  the block journals the Units of every other member on that position.
* **Changing the preferred Edition writes positions and read marks without the Work's lease.**
  `POST /api/v1/editions/{id}/preferred` carries every profile's reading over (`carry_over`): a
  `transposed` position per profile and its read marks (which the view keeps, even on a partial
  Unit). It runs in the request, under no lock. The lot says every writer of a position takes the
  lease of the Work; this one writes the very marks the guard "chapter read" of an upgrade looks
  at, in the window between that guard and the swap.
* **A partial cover that holds more pages than the Unit's whole file still reaches the fraction
  1.0.** The fix of the first review divides the pages read in the partial file by the span of a
  file that covers the Unit whole, then caps the result at 1.0. When the partial file is paged
  more finely than the whole file (split spreads), its pages are not the whole file's pages: the
  quotient goes past 1.0, is capped, and the partial position outranks a position further into the
  whole file again, which is the hole the first review closed for the other case.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.locks import ResourceLocks, work_resource
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.bundle_env import Chapter
from tests.integration.test_l47_amendments import _back_by, _read, floor4
from tests.integration.test_l47_kosync import block, library, push
from tests.integration.test_l48_opds import World as OpdsWorld
from tests.integration.test_l48_opds import make_world
from tests.integration.test_m4_l15_edition_change_api import World as EditionWorld
from tests.integration.test_m5_l22a_progress_view import _partial
from tests.reading_env import NEW_HASH, T0, hours, reading_env
from tests.visibility_env import add_profile, seed_catalogue

xfail = pytest.mark.xfail(strict=True, reason="#386 second review: hole not fixed yet")


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[OpdsWorld]:
    with make_world(monkeypatch, settings) as made:
        yield made


@pytest.fixture
def editions(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[EditionWorld]:
    from tests.auth_env import auth_env
    from tests.reading_env import ReadingEnv

    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200
        seed = seed_catalogue(env)
        kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        member = add_profile(env, "mum", "member", "10.0.0.3")
        yield EditionWorld(env, ReadingEnv(seed.engine, seed), kid, member)
        seed.engine.dispose()


@xfail
@pytest.mark.req("EF-LEC-014", "EF-LEC-009")
def test_a_walk_through_a_block_never_reads_a_unit_its_file_covers_in_part(
    lab: OpdsWorld,
) -> None:
    """Twenty chapters of ten pages in one block; the third chapter's file holds only part of it
    (`extent = 'partial'`) and no other file covers it whole. An hour from page 1 to page 85 reads
    the chapters crossed, but the third one is at most `in_progress`: the rest of it is in no
    file's pages, as 0106 says of every walk."""
    lib = library(lab)
    items = [lib.add(Chapter.simple(str(n), count=10)) for n in range(1, 21)]  # 200 pages
    with lab.seed.engine.begin() as connection:
        connection.execute(
            sa.update(export.unit_coverage)
            .where(
                export.unit_coverage.c.carrier_id == items[2].item_id,
                export.unit_coverage.c.unit_id == items[2].unit_id,
            )
            .values(extent="partial")
        )
    digest, _ = block(lab, lab.mum)

    assert push(lab.mum, digest, "1", floor4(1, 200)).status_code == 200
    _back_by(lab, timedelta(hours=1))
    assert push(lab.mum, digest, "85", floor4(85, 200)).status_code == 200

    read = _read(lab)
    assert {items[0].unit_id, items[1].unit_id, items[3].unit_id} <= read  # the walk did count
    assert items[2].unit_id not in read  # but never the Unit its file holds only in part


@xfail
@pytest.mark.req("EF-BIB-026", "EF-LEC-014")
def test_changing_the_preferred_edition_writes_no_position_while_a_job_holds_the_work(
    editions: EditionWorld,
) -> None:
    """An upgrade holds the Work (`work:<id>`, M4 L35) between its guard "chapter read" and its
    swap. The administrator changes the preferred Edition meanwhile: the carry over writes a
    position and read marks for each profile on the new Edition's files. Like every other writer
    of a position it must wait for the lease, and write nothing while it stays held."""
    world = editions
    before = world.state()[1]
    locks = ResourceLocks(world.reading.engine, clock=world.env.clock)
    with locks.hold_resources(new_ulid(), [work_resource(world.work)]):
        world.env.post(world.url(world.fr), json={})
        assert world.state()[1] == before  # no position, no mark, under the job's lease


@xfail
@pytest.mark.req("EF-LEC-014")
def test_a_finely_paged_partial_cover_never_reaches_the_end_of_the_unit(tmp_path: Path) -> None:
    """The chapter's own file holds it whole in ten pages (spreads); a volume holds part of it in
    thirty single pages (`extent = 'partial'`). The member reads the whole file to page 9, then
    page 25 of the volume. Whatever the volume's pages are worth, they are not the end of the
    Unit: her place in the whole file stays the furthest one."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        (unit,) = env.units("ok", 1)
        whole_hash = "sha256:" + "d5" * 32
        whole = env.file("ok", [unit], bounds=[(1, 10)], content_hash=whole_hash)
        volume = env.file("ok", [unit], bounds=[(1, 30)], content_hash=NEW_HASH)
        _partial(env, volume, unit)

        read_whole = env.position(
            reader, whole, page=9, percent=None, client_ts=T0, content_hash=whole_hash
        )
        env.position(reader, volume, page=25, percent=None, client_ts=hours(1))

        row = env.progress(reader)[unit]
        assert row.state == "in_progress"
        assert row.last_position_id == read_whole
