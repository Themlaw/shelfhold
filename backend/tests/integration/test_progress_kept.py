"""A delete, a rename, a reshelving or a binding migration inside one root keeps each member's
progress (M4 L16, EF-EXP-017, EF-EXP-030): pull, move, scan, resolve, push, with the key of every
member.

The files and the manifest are real; Kavita is simulated and **indexes the disk again at each scan
the plan asks for**: a renamed folder is a new series with new chapter identifiers, and the old
one is deleted together with every reading of every account. What a member read only in Kavita is
pulled before the move, what Shelfhold alone holds is pushed after it, and nothing is ever made
"read" on the way. Attacks: a member without a key, two members, a child, a collision of names, a
move across two roots, a stop at every step.
"""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.rename_plan import (
    ItemMove,
    RenameConfirmationRequiredError,
    RenameRefusedError,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export
from tests.carry_env import PAGES, CarryEnv, carry_env
from tests.export_env import add_second_shelf
from tests.rename_env import plan_status, resume_plan, tree

pytestmark = pytest.mark.req("EF-EXP-017", "EF-EXP-030")

OLD, NEW = "Dr Stone", "Dr Stone Renamed"
CH1, CH2, CH3 = (f"{OLD} - Ch.{n}.cbz" for n in "123")


@pytest.fixture
def cenv() -> Iterator[CarryEnv]:
    with carry_env() as made:
        yield made


def _apply(cenv: CarryEnv, preview: Any, *, confirm: bool = False) -> None:
    cenv.env.pipeline.apply(
        preview.plan_id, if_match=preview.base_version, job_id=new_ulid(),
        confirm_progress_loss=confirm,
    )  # fmt: skip


def _setup(cenv: CarryEnv) -> tuple[str, str, dict[str, str]]:
    """Two members of the same Kavita; alice read chapter 1 in Kavita and chapter 3 on a reader
    Shelfhold alone knows; bob read chapter 2 in Kavita."""
    ids = cenv.series(OLD, ["1", "2", "3"])
    alice, bob = cenv.member("alice"), cenv.member("bob")
    cenv.read_in_kavita(alice, OLD, CH1, 12)
    cenv.read_in_kavita(bob, OLD, CH2, 7)
    cenv.device_position(alice, ids[CH3], 5)
    cenv.wire()
    return alice, bob, ids


def _new_chapter_pages(cenv: CarryEnv, folder: str) -> dict[str, dict[str, int]]:
    return {
        user: {name: cenv.kavita_page(user, folder, name) for name in (CH1, CH2, CH3)}
        for user in cenv.names
    }


def test_delete_and_rename_resolve_remote_preserves_progress(cenv: CarryEnv) -> None:
    alice, bob, _ids = _setup(cenv)
    old_series = cenv.series_ids()[OLD]
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    assert preview.warnings == ()  # every reader has a key: nobody loses anything
    _apply(cenv, preview)  # no confirmation was needed
    series = cenv.series_ids()
    assert OLD not in series and series[NEW] != old_series  # deleted, and found again, new id
    assert all(chapter // 100 != old_series for _acct, chapter in cenv.fake.member_progress)
    pages = _new_chapter_pages(cenv, NEW)
    assert pages[alice] == {CH1: 12, CH2: 0, CH3: 5}
    assert pages[bob] == {CH1: 0, CH2: 7, CH3: 0}
    with cenv.engine.connect() as connection:  # the cached identity follows the series
        cached = connection.execute(sa.select(export.export_series.c.kavita_series_id)).scalars()
        assert {str(x) for x in cached if x} == {str(series[NEW])}
    for user in (alice, bob):  # nothing became "read" on the way
        assert {state for state, _ in cenv.progress(user).values()} <= {"in_progress"}
    before = {user: len(cenv.positions(user)) for user in (alice, bob)}
    cenv.sync_all()  # the next pass finds its own writes as echoes, never as readings
    assert {user: len(cenv.positions(user)) for user in (alice, bob)} == before


def test_reshelving_preserves_progress_pull_move_scan_resolve_push(cenv: CarryEnv) -> None:
    alice, bob, _ids = _setup(cenv)
    cenv.watch()
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    _apply(cenv, preview)
    kinds = cenv.kinds()
    assert {"pull", "scan", "push"} <= set(kinds)
    first_pull, scan = kinds.index("pull"), kinds.index("scan")
    first_push = kinds.index("push")
    assert first_pull < scan < first_push  # pull, then the scan, then the push
    assert "push" not in kinds[:scan]  # nothing is written in Kavita before the move is scanned
    old_files = {f"{OLD}/{n}" for n in (CH1, CH2, CH3)}
    new_files = {f"{NEW}/{n}" for n in (CH1, CH2, CH3)}
    first_pull = cenv.log[kinds.index("pull")][1]
    assert old_files <= set(first_pull) and not new_files & set(first_pull)  # before the move
    at_scan = cenv.log[scan][1]
    assert new_files <= set(at_scan) and not old_files & set(at_scan)  # the move is done
    assert new_files <= set(cenv.log[first_push][1])
    assert _new_chapter_pages(cenv, NEW)[alice][CH1] == 12
    assert _new_chapter_pages(cenv, NEW)[bob][CH2] == 7
    assert tree(cenv.env) == sorted(new_files)


def test_member_without_token_named_in_preview(cenv: CarryEnv) -> None:
    ids = cenv.series(OLD, ["1", "2"])
    cenv.member("alice")  # a key
    carol = cenv.member("carol", link=False)  # an account in Kavita, no key
    cenv.member("dave", link=False, library_access=False)  # no access to this library
    cenv.member("erin", link=False, kavita=False)  # no Kavita account at all
    cenv.wire()
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    assert len(preview.warnings) == 1
    warning = preview.warnings[0]
    assert warning.code == "progress_loss"
    assert warning.members == ("carol",)  # named, alone: the others are carried or out of reach
    assert warning.readers >= 1
    with pytest.raises(RenameConfirmationRequiredError):  # the warning is hers, and it is asked
        _apply(cenv, preview)
    assert tree(cenv.env) == sorted(f"{OLD}/{name}" for name in ids)  # nothing moved
    cenv.link(carol, "acct-carol")  # she pastes her key
    again = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    assert again.warnings == ()
    _apply(cenv, again)  # and nobody is asked anything any more


def test_rename_collision_checked_at_planning(cenv: CarryEnv) -> None:
    ids = cenv.series(OLD, ["1", "2"])
    cenv.member("alice")
    cenv.wire()
    cenv.fake.add_series(
        "Blue Box", "/manga/Kavita/Elsewhere", ["/manga/Kavita/Elsewhere/x.cbz"], library_id=1
    )  # a series nobody from Shelfhold wrote, under the name the plan would give
    before = (tree(cenv.env), cenv.fake.series[:])
    with pytest.raises(RenameRefusedError) as refused:
        cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, "Blue Box"))
    assert refused.value.reason == "series_name_collision"
    with cenv.engine.connect() as connection:  # nothing is stored, nothing moved
        assert connection.execute(
            sa.select(sa.func.count()).select_from(coherence.export_plan)
            .where(coherence.export_plan.c.kind == "rename_plan")
        ).scalar_one() == 0  # fmt: skip
    assert (tree(cenv.env), cenv.fake.series) == before
    # the name Kavita shows for **our own** series is not a collision (the user renamed it there)
    own = next(s for s in cenv.fake.series if s["name"] == OLD)
    own["name"] = "Doctor Stone"
    preview = cenv.env.pipeline.rename_preview(
        cenv.env.destination_id, moves=cenv.moves(OLD, "Doctor Stone")
    )
    assert preview.plan_id is not None and len(ids) == 2


def test_cross_root_binding_migration_still_refused_with_code(cenv: CarryEnv) -> None:
    cenv.series(OLD, ["1", "2"])
    cenv.wire()
    env = cenv.env
    shelf, second = add_second_shelf(env)
    other = env.new_series("Blue Box", shelf_id=shelf)
    env.add_chapter(other, "1")
    env.pipeline.run(second, job_id=new_ulid())
    with env.engine.connect() as connection:
        foreign = str(
            connection.execute(
                sa.select(export.export_record.c.export_item_id).where(
                    export.export_record.c.shelf_destination_id == second
                )
            ).scalars().first()
        )  # fmt: skip
    before = tree(env)
    with pytest.raises(RenameRefusedError) as refused:  # an item of the other root
        env.pipeline.rename_preview(env.destination_id, moves=[ItemMove(foreign, f"{NEW}/x.cbz")])
    assert refused.value.reason == "cross_root"
    with pytest.raises(RenameRefusedError) as named:  # a move that names the other destination
        env.pipeline.rename_preview(
            env.destination_id,
            moves=[ItemMove(next(iter(cenv.moves(OLD, NEW))).export_item_id, f"{NEW}/x.cbz",
                            destination_id=second)],
        )  # fmt: skip
    assert named.value.reason == "cross_root"
    assert tree(env) == before


# -- what is never done ------------------------------------------------------------------------


def test_a_look_at_the_last_page_and_a_transposed_position_never_become_read(
    cenv: CarryEnv,
) -> None:
    ids = cenv.series(OLD, ["1", "2", "3"])
    alice = cenv.member("alice")
    cenv.device_position(alice, ids[CH1], PAGES - 1)  # a glance at the end, not a reading
    cenv.device_position(alice, ids[CH2], 15, transposed=True)  # a guess from another Edition
    cenv.read_in_kavita(alice, OLD, CH3, PAGES)  # Kavita itself says read: that one is proven
    cenv.wire()
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    _apply(cenv, preview)
    pages = _new_chapter_pages(cenv, NEW)[alice]
    assert pages[CH1] == PAGES - 2  # never the count: a look is given as a page
    assert pages[CH2] == 0  # a transposed position is never a page to push
    assert pages[CH3] == PAGES  # what Kavita called read comes back as read
    assert cenv.progress(alice)  # and Shelfhold still holds what it held


def test_a_member_is_never_written_with_another_ones_key(cenv: CarryEnv) -> None:
    alice, bob, ids = _setup(cenv)
    carol = cenv.member("carol", link=False)  # an account in Kavita, no key on this instance
    cenv.device_position(carol, ids[CH1], 9)
    carol_before = cenv.positions(carol)
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    assert preview.warnings[0].members == ("carol",)
    _apply(cenv, preview, confirm=True)  # the warning is hers: the admin gave the confirmation
    writers = {account for account, _chapter, _page in cenv.fake.progress_writes}
    assert writers == {"acct-alice", "acct-bob"}  # each one with their own key, nobody else
    assert cenv.kavita_page(alice, NEW, CH2) == 0  # bob's reading never went to alice
    assert cenv.kavita_page(bob, NEW, CH1) == 0 and cenv.kavita_page(bob, NEW, CH3) == 0
    assert "acct-carol" not in {account for account, _chapter in cenv.fake.member_progress}
    assert cenv.positions(carol) == carol_before  # and her own positions are not touched


def test_a_child_chapter_above_the_ceiling_is_neither_pulled_nor_pushed(cenv: CarryEnv) -> None:
    ids = cenv.series(OLD, ["1"])
    hidden = cenv.series("Blue Box", ["1"])
    child = cenv.member("kid", role="child")
    with cenv.engine.begin() as connection:  # the second series is rated above the ceiling
        connection.execute(
            sa.text("UPDATE work SET age_rating = 6, rated_at = '2026-11-01 00:00:00'")
        )
        connection.execute(
            sa.text("UPDATE edition SET age_rating = 6, rated_at = '2026-11-01 00:00:00'")
        )
        connection.execute(
            sa.text(
                "UPDATE edition SET age_rating = 16 WHERE id IN (SELECT i.edition_id FROM "
                "export_item i JOIN export_record r ON r.export_item_id = i.id "
                "WHERE r.rel_path LIKE 'Blue Box/%')"
            )
        )
        connection.execute(
            sa.text("UPDATE work SET age_rating = 16 WHERE id IN "
                    "(SELECT work_id FROM edition WHERE age_rating = 16)")
        )  # fmt: skip
    (blue,) = hidden
    blue_chapter = cenv.chapter("Blue Box", blue)
    cenv.read_in_kavita(child, "Blue Box", blue, 6)  # in Kavita, above the ceiling
    cenv.read_in_kavita(child, OLD, CH1, 4)  # in Kavita, visible
    cenv.wire()
    moves = cenv.moves(OLD, NEW) + cenv.moves("Blue Box", "Blue Box Renamed")
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=moves)
    _apply(cenv, preview, confirm=True)
    asked = [r for r in cenv.fake.requests_with_key(cenv.keys[child])
             if "reader/get-progress" in r.path.lower()]  # fmt: skip
    assert asked  # the visible chapter was asked about
    chapters_asked = {int(r.query["chapterId"]) for r in asked if r.query.get("chapterId")}
    assert blue_chapter not in chapters_asked
    stored = [p for p in cenv.positions(child) if p.source == "server"]
    assert {p.carrier_id for p in stored} == set(ids.values())  # the visible one, nothing else
    assert cenv.kavita_page(child, NEW, CH1) == 4  # carried
    assert cenv.kavita_page(child, "Blue Box Renamed", blue) == 0  # a hidden one is not pushed


# -- a stop at every step ----------------------------------------------------------------------


class Killed(BaseException):
    """kill -9: nothing after the point runs, no handler settles anything."""


@pytest.mark.req("EF-EXP-041", "EF-EXP-030")
@pytest.mark.parametrize("tick", range(48))
def test_a_stop_at_any_step_resumes_with_every_progress_kept(
    cenv: CarryEnv, monkeypatch: pytest.MonkeyPatch, tick: int
) -> None:
    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.export.application.plans import PlanStore

    alice, bob, _ids = _setup(cenv)
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    plan_id = preview.plan_id
    assert plan_id is not None
    seen = {"n": 0, "killed": False}

    def step() -> None:
        if seen["n"] == tick and not seen["killed"]:
            seen["killed"] = True
            raise Killed
        seen["n"] += 1

    # one tick for each call to Kavita, each scan, each rename, each journal line, each op done
    previous = cenv.fake.hook

    def hook(recorded: Any) -> None:
        step()
        if previous is not None:
            previous(recorded)

    cenv.fake.hook = hook
    real_rename, real_set_op = cenv.env.lib.fsops.rename, PlanStore._set_op
    real_record = IntentJournal.record

    def rename(src: str, dst: str, **k: Any) -> None:
        step()
        real_rename(src, dst, **k)
        step()

    def record(self: Any, *a: Any, **k: Any) -> str:
        step()
        return real_record(self, *a, **k)

    def set_op(self: Any, op_id: str, status: str, *reason: Any) -> None:
        if status == "done":
            step()
        real_set_op(self, op_id, status, *reason)

    monkeypatch.setattr(cenv.env.lib.fsops, "rename", rename)
    monkeypatch.setattr(IntentJournal, "record", record)
    monkeypatch.setattr(PlanStore, "_set_op", set_op)
    try:
        _apply(cenv, preview)
    except Killed:
        monkeypatch.undo()
        cenv.fake.hook = previous
        with cenv.engine.begin() as connection:  # a killed process runs no handler
            connection.execute(sa.update(coherence.export_plan).values(status="applying"))
            connection.execute(
                sa.update(coherence.export_op)
                .where(coherence.export_op.c.status == "failed")
                .values(status="pending")
            )
        resume_plan(cenv.env, plan_id)
    assert plan_status(cenv.env, plan_id) == "done"
    pages = _new_chapter_pages(cenv, NEW)
    assert pages[alice] == {CH1: 12, CH2: 0, CH3: 5}  # whatever the step, all of it is back
    assert pages[bob] == {CH1: 0, CH2: 7, CH3: 0}
    assert tree(cenv.env) == sorted(f"{NEW}/{n}" for n in (CH1, CH2, CH3))
    with cenv.engine.connect() as connection:
        open_intents = connection.execute(
            sa.select(sa.func.count())
            .select_from(coherence.fs_intent)
            .where(coherence.fs_intent.c.state.in_(("pending", "applying")))
        ).scalar_one()
    assert open_intents == 0
    for user in (alice, bob):  # no duplicate, no invented reading
        assert {state for state, _ in cenv.progress(user).values()} <= {"in_progress"}
        servers = [p for p in cenv.positions(user) if p.source == "server"]
        assert len({(p.carrier_id, p.locator["page"]) for p in servers}) == len(servers)


# -- a rescan that changes the series identifier ----------------------------------------------


def test_rescan_that_changes_the_series_id_is_healed_by_the_next_pass(cenv: CarryEnv) -> None:
    alice, bob, _ids = _setup(cenv)
    cenv.sync_all()  # what each member read in Kavita is in Shelfhold now
    old = cenv.series_ids()[OLD]
    files = list(cenv.fake.files[old])
    cenv.fake.series[:] = [s for s in cenv.fake.series if s["id"] != old]  # Kavita drops the series
    cenv.fake.files.pop(old)
    for key in [k for k in cenv.fake.member_progress if k[1] // 100 == old]:
        del cenv.fake.member_progress[key]  # ... and every reading with it
    cenv.fake.add_series(OLD, "/manga/Kavita/" + OLD, files, library_id=1)  # a new id
    assert cenv.series_ids()[OLD] != old
    cenv.sync_all()
    pages = _new_chapter_pages(cenv, OLD)
    assert pages[alice] == {CH1: 12, CH2: 0, CH3: 5}
    assert pages[bob] == {CH1: 0, CH2: 7, CH3: 0}
    assert {state for state, _ in cenv.progress(alice).values()} <= {"in_progress"}


def test_a_key_kavita_refuses_stops_the_batch_before_anything_moves(cenv: CarryEnv) -> None:
    alice, bob, _ids = _setup(cenv)
    cenv.fake.auth_keys[:] = [k for k in cenv.fake.auth_keys if k["key"] != cenv.keys[alice]]
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    with pytest.raises(RenameRefusedError) as refused:  # alice's key is dead: her reading is
        _apply(cenv, preview)  # only in Kavita, and the scan would delete it
    assert refused.value.reason == "progress_not_pulled"
    assert tree(cenv.env) == sorted(f"{OLD}/{n}" for n in (CH1, CH2, CH3))  # nothing moved
    assert cenv.kavita_page(alice, OLD, CH1) == 12 and cenv.kavita_page(bob, OLD, CH2) == 7
    assert preview.plan_id is not None
    assert plan_status(cenv.env, preview.plan_id) == "failed"


# -- a Kavita that scans late, or not at all --------------------------------------------------


def test_a_kavita_that_scans_late_is_waited_for(cenv: CarryEnv) -> None:
    alice, bob, _ids = _setup(cenv)
    cenv.wire(drain=lambda: None)  # the scan is asked and Kavita does not answer yet
    cenv.on_sleep = cenv.rescan  # it indexes the disk while the carry waits
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    _apply(cenv, preview)
    assert len(cenv.sleeps) >= 1  # it waited, a bounded number of times
    pages = _new_chapter_pages(cenv, NEW)
    assert pages[alice] == {CH1: 12, CH2: 0, CH3: 5}
    assert pages[bob] == {CH1: 0, CH2: 7, CH3: 0}


def test_a_scan_that_never_comes_leaves_the_rest_to_the_periodic_pass(cenv: CarryEnv) -> None:
    alice, bob, _ids = _setup(cenv)
    cenv.wire(drain=lambda: None)
    preview = cenv.env.pipeline.rename_preview(cenv.env.destination_id, moves=cenv.moves(OLD, NEW))
    _apply(cenv, preview)  # the plan is done: the files are where they were asked to be
    assert plan_status(cenv.env, str(preview.plan_id)) == "done"
    assert len(cenv.sleeps) == 5  # six tries, then it gives up: bounded, never a hang
    cenv.rescan()  # Kavita scans at last: the old series and its readings are gone
    assert cenv.series_ids().get(OLD) is None
    cenv.sync_all()  # the periodic pass finds chapters it has never seen, and carries them
    pages = _new_chapter_pages(cenv, NEW)
    assert pages[alice] == {CH1: 12, CH2: 0, CH3: 5}
    assert pages[bob] == {CH1: 0, CH2: 7, CH3: 0}
