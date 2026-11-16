"""Relecture adverse de #312 (M4 L16): the progress carried over a rename plan, attacked.

Each hole found is a red test in strict xfail: it turns green (and the xfail fails) the day it is
closed. The process-kill test is a real `SIGKILL` (a forked child killed by the kernel, no handler
runs), and is expected to pass: the pulled progress lives in the database, committed chapter by
chapter, before the first move.
"""

import os
import signal
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.rename_plan import ItemMove, RenameRefusedError
from shelfhold.infrastructure.ids import new_ulid
from tests.carry_env import PAGES, CarryEnv, carry_env
from tests.rename_env import plan_status, resume_plan, tree

pytestmark = pytest.mark.req("EF-EXP-017", "EF-EXP-030")

OLD, NEW = "Dr Stone", "Dr Stone Renamed"
CH1, CH2, CH3 = (f"{OLD} - Ch.{n}.cbz" for n in "123")


@pytest.fixture
def cenv() -> Iterator[CarryEnv]:
    with carry_env() as made:
        yield made


def _apply(cenv: CarryEnv, preview: Any) -> None:
    cenv.env.pipeline.apply(
        preview.plan_id, if_match=preview.base_version, job_id=new_ulid(),
        confirm_progress_loss=False,
    )  # fmt: skip


def _renumber(cenv: CarryEnv, folder: str) -> None:
    """Kavita drops the series and its readings and indexes the same files under a new id."""
    old = cenv.series_ids()[folder]
    files = list(cenv.fake.files[old])
    cenv.fake.series[:] = [s for s in cenv.fake.series if s["id"] != old]
    cenv.fake.files.pop(old)
    for key in [k for k in cenv.fake.member_progress if k[1] // 100 == old]:
        del cenv.fake.member_progress[key]
    cenv.fake.add_series(folder, "/manga/Kavita/" + folder, files, library_id=1)
    assert cenv.series_ids()[folder] != old


# -- 1. a real kill -9 between the pull and the push -------------------------------------------


@pytest.mark.parametrize("point", ["before_first_move", "before_restore"])
def test_a_real_sigkill_between_pull_and_push_loses_nothing(cenv: CarryEnv, point: str) -> None:
    ids = cenv.series(OLD, ["1", "2", "3"])
    alice, bob = cenv.member("alice"), cenv.member("bob")
    cenv.read_in_kavita(alice, OLD, CH1, 12)
    cenv.read_in_kavita(bob, OLD, CH2, 7)
    cenv.device_position(alice, ids[CH3], 5)
    cenv.wire()
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    plan_id = str(preview.plan_id)

    pid = os.fork()
    if pid == 0:  # the child: the real process of the apply, killed by the kernel
        try:
            cenv.engine.dispose(close=False)  # never the parent's pooled connections

            def die(*_a: Any, **_k: Any) -> Any:
                os.kill(os.getpid(), signal.SIGKILL)

            if point == "before_first_move":
                cenv.env.lib.fsops.rename = die  # type: ignore[method-assign]
            else:
                cenv.carry.restore = die  # type: ignore[method-assign]
            _apply(cenv, preview)
        finally:
            os._exit(3)  # never reached when the kill came: a test that did not kill fails
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    cenv.engine.dispose()

    # meanwhile Kavita indexes the disk on its own (folder watching): whatever only it held for
    # a series it lost is gone; the parent's simulated Kavita never saw the child's reads
    cenv.rescan()
    cenv.env.clock.advance(minutes=11)  # the lease of the killed job runs out (or a boot frees it)
    resume_plan(cenv.env, plan_id)
    assert plan_status(cenv.env, plan_id) == "done"
    assert tree(cenv.env) == sorted(f"{NEW}/{n}" for n in (CH1, CH2, CH3))
    assert cenv.kavita_page(alice, NEW, CH1) == 12
    assert cenv.kavita_page(alice, NEW, CH3) == 5
    assert cenv.kavita_page(bob, NEW, CH2) == 7


# -- 1. a renumbered chapter left to the periodic pass ------------------------------------------


def test_poc_a_renumbered_chapter_gets_the_furthest_page_not_the_last_device_one(
    cenv: CarryEnv,
) -> None:
    ids = cenv.series(OLD, ["1"])
    alice = cenv.member("alice")
    cenv.device_position(alice, ids[CH1], 3)  # a reader of Shelfhold, early on
    cenv.sync_all()
    assert cenv.kavita_page(alice, OLD, CH1) == 3
    cenv.env.clock.advance(seconds=3600)
    cenv.read_in_kavita(alice, OLD, CH1, 15)  # then she goes on in Kavita's own reader
    cenv.sync_all()  # pulled: Shelfhold proves page 15
    _renumber(cenv, OLD)  # a rescan, or a plan whose scan came after the six tries
    cenv.sync_all()
    assert cenv.kavita_page(alice, OLD, CH1) == 15  # actual: 3


def test_poc_a_late_scan_after_a_plan_keeps_the_furthest_page(cenv: CarryEnv) -> None:
    ids = cenv.series(OLD, ["1"])
    alice = cenv.member("alice")
    cenv.device_position(alice, ids[CH1], 3)
    cenv.sync_all()
    cenv.env.clock.advance(seconds=3600)
    cenv.read_in_kavita(alice, OLD, CH1, 15)
    cenv.wire(drain=lambda: None)  # Kavita does not scan within the wait
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    _apply(cenv, preview)
    cenv.rescan()  # it scans at last: the old series and its readings are gone
    cenv.sync_all()
    assert cenv.kavita_page(alice, NEW, CH1) == 15  # actual: 3


# -- 2. a false "read" -------------------------------------------------------------------------


def test_poc_a_chapter_marked_unread_in_kavita_is_not_made_read_by_the_carry(
    cenv: CarryEnv,
) -> None:
    cenv.series(OLD, ["1"])
    alice = cenv.member("alice")
    cenv.read_in_kavita(alice, OLD, CH1, PAGES)  # read to the end in Kavita
    cenv.sync_all()
    cenv.env.clock.advance(seconds=3600)
    cenv.read_in_kavita(alice, OLD, CH1, 0)  # then "mark as unread" in Kavita
    cenv.sync_all()
    cenv.wire()
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    _apply(cenv, preview)
    assert cenv.kavita_page(alice, NEW, CH1) < PAGES  # actual: PAGES, « read » again


def test_poc_a_chapter_marked_unread_is_not_made_read_by_the_periodic_pass(
    cenv: CarryEnv,
) -> None:
    cenv.series(OLD, ["1"])
    alice = cenv.member("alice")
    cenv.read_in_kavita(alice, OLD, CH1, PAGES)
    cenv.sync_all()
    cenv.env.clock.advance(seconds=3600)
    cenv.read_in_kavita(alice, OLD, CH1, 0)
    cenv.sync_all()
    _renumber(cenv, OLD)
    cenv.sync_all()
    assert cenv.kavita_page(alice, OLD, CH1) < PAGES  # actual: PAGES


# -- 1. two series of ours merged into one folder ----------------------------------------------


def test_poc_two_series_of_the_plan_into_one_new_folder_are_refused(cenv: CarryEnv) -> None:
    first = cenv.series(OLD, ["1"])
    other = cenv.series("Blue Box", ["1"])
    cenv.member("alice")
    cenv.wire()
    moves = [ItemMove(i, f"Merged/{name}") for name, i in {**first, **other}.items()]
    with pytest.raises(RenameRefusedError) as refused:
        cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=moves)
    assert refused.value.reason == "series_name_collision"


def test_no_member_is_pushed_with_a_key_that_is_not_theirs(cenv: CarryEnv) -> None:
    """IDOR: every progress write of the carry is made with the key of the account it is for."""
    ids = cenv.series(OLD, ["1", "2"])
    alice, bob = cenv.member("alice"), cenv.member("bob")
    cenv.device_position(alice, ids[CH1], 6)
    cenv.device_position(bob, ids[CH2], 4)
    cenv.wire()
    writes: list[tuple[str | None, int]] = []
    previous = cenv.fake.hook

    def hook(recorded: Any) -> None:
        if recorded.path.lower().endswith("/api/reader/progress") and recorded.method == "POST":
            writes.append((cenv.fake._account_of(recorded), len(writes)))
        if previous is not None:
            previous(recorded)

    cenv.fake.hook = hook
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    _apply(cenv, preview)
    assert writes and {who for who, _ in writes} <= {"acct-alice", "acct-bob"}
    assert cenv.kavita_page(bob, NEW, CH1) == 0 and cenv.kavita_page(alice, NEW, CH2) == 0
    with cenv.engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM reading_position")).scalar()
