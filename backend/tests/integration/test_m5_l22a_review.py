"""The adverse review of M5 L22a (M4 debts): the proofs of what this re-reading found.

Three holes, one proof each, red until they are fixed (`xfail(strict=True)`):

* **A Unit covered in part is ranked by the fraction of the covered part, not of the Unit.** The
  frozen rule of `reading_progress` says the furthest position of a Unit wins on "the fraction
  of the Unit reached, never the percent of two different files"
  (`docs/reference/reading-progress.md`).
  A file that covers the Unit only in part says nothing of the rest of it (0106 caps its state at
  `in_progress`), but the view still computes `within` over the pages the file holds: reaching the
  end of a quarter of the chapter counts as 1.0 and outranks a position further into the chapter
  on a file that covers it whole. The member's whole file stops being the furthest, and what
  `last_position_id` feeds (the kosync `GET`, the carry over to a preferred Edition) answers from
  the wrong place: her own, further position is lost from every consumer of "the furthest".
* **A cancelled assembly restores whatever sits in the bin entry.** `_cancel_assembly` checks that
  the served path is free and that *a* file is in the entry the intent named, never that it is the
  chapter the intent took (its sha256 is in the intent's own payload). The same third party the
  feature is about — the one that removed the volume — may have left other bytes there: they are
  moved to the chapter's served path, the intent closes `cancelled`, and the manifest still says
  the served file has the chapter's hash: a file served under a hash that is not its own.
* **Settling a conflict writes a position without the Work's lease.** Every writer of a position
  takes the lease (the reader's `PUT`, kosync, the Kavita pull), and so does the `cfi` write
  although it marks nothing: "one write path, not two rules" (the lot's choice 3).
  `resolve(choice=loser)` writes a position through a second rule: while a job holds the Work, the
  route answers 200 and stores the place the reader chose to resume at.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.export.application.intents import OPEN_STATES
from shelfhold.infrastructure.schema import reading
from tests.assembly_env import bin_files
from tests.auth_env import CSRF_HEADER
from tests.export_env import export_env
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import count, kept, page, put
from tests.integration.test_m5_l22a_cancelled_assembly import (
    _cut_after_one_chapter,
    _intent_state,
    _resolve,
)
from tests.integration.test_m5_l22a_lease import _holding
from tests.integration.test_m5_l22a_progress_view import _partial
from tests.reading_env import NEW_HASH, T0, hours, reading_env


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


@pytest.mark.req("EF-LEC-014")
def test_a_whole_file_read_further_stays_the_furthest_position_of_a_partly_covered_unit(
    tmp_path: Path,
) -> None:
    """The volume holds pages 1 to 10 of a 40-page chapter (`extent = 'partial'`), the chapter's own
    file covers it whole. The member finishes the volume (a quarter of the chapter), then reads
    further in her own whole file: the frozen rule says the furthest position of a Unit is decided
    on the fraction **of the Unit** reached, so her own position must stay the furthest one — the
    volume, read to the end of what it holds, never overtakes it."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        (unit,) = env.units("ok", 1)
        volume = env.file("ok", [unit], bounds=[(1, 10)], content_hash=NEW_HASH)
        _partial(env, volume, unit)
        whole = env.file("ok", [unit], bounds=[(1, 40)], content_hash="sha256:" + "d4" * 32)

        env.position(reader, volume, page=10, percent=None, client_ts=T0)
        read_whole = env.position(
            reader, whole, page=20, percent=None, client_ts=hours(1),
            content_hash="sha256:" + "d4" * 32,
        )  # fmt: skip

        row = env.progress(reader)[unit]
        assert row.state == "in_progress"  # the state rule of 0106 holds for both files
        assert row.last_position_id == read_whole  # the fraction of the Unit, not of the cover


@pytest.mark.req("EF-EXP-019", "EF-BIB-044")
def test_a_cancelled_assembly_never_serves_what_sits_in_the_chapters_bin_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The volume is gone (the third party the feature is about) and the bin entry of the first
    chapter holds other bytes than the chapter's own: whatever is there is not "the same file, the
    same inode" that the cancel promises to put back, so the intent is drift, nothing is moved to
    the served path, and the chapter stays in its entry for an admin to look at."""
    with export_env() as env:
        targets, intent_id = _cut_after_one_chapter(env, monkeypatch)
        (volume,) = [p for p in env.files() if p not in targets]
        volume.unlink()
        (binned,) = bin_files(env)
        binned.write_bytes(b"not the chapter any more")

        report = _resolve(env, intent_id)

        assert intent_id in report.drifted  # type: ignore[attr-defined]
        assert _intent_state(env, intent_id) in OPEN_STATES
        assert not targets[0].exists()  # nothing of the bin reaches the served path
        assert bin_files(env) == [binned]  # and the entry is left exactly as it was


@pytest.mark.req("EF-LEC-007", "EF-LEC-014")
def test_settling_a_conflict_writes_no_position_while_a_job_holds_the_work(
    world: World,
) -> None:
    """An upgrade holds the Work (the lease of M4 L35, `work:<id>`): the reader's own `PUT` and
    kosync answer `progress_busy` and write nothing. Settling the open proposal the other way
    round (`choice = loser`) writes a position too, so the same lease must answer the same 503 —
    one write path, not two rules (the lot's choice 3, the `cfi` write)."""
    content, _unit = kept(world)
    assert put(world, world.admin, content, page(20, 50), device="tab", minutes=-30).is_success
    proposal = put(world, world.admin, content, page(2, 50), device="phone", minutes=-10).json()
    assert proposal["conflict"] is not None

    with _holding(world, world.seed.works["ok"]):
        settled = world.admin.post(
            f"/api/reader/conflicts/{proposal['conflict']['id']}/resolve",
            json={"choice": "loser", "device_id": "phone"},
            headers={CSRF_HEADER: world.env.csrf(world.admin)},
        )
        assert settled.status_code == 503, settled.text
        assert count(world, reading.reading_position) == 2  # nothing written under the hold
