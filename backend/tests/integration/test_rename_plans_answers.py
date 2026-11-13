"""The answers to the contradictor of #239 (M3 L29), one test per decision: a chain of renames
ordered or given back, I9 in both directions, a key that cannot replay the application, the refusals
of the disk, one application of a plan at a time, the names of an export, a confirmation that is for
the count it was given for, no count in a log, one scan per batch and a rescan on the resume.
See `docs/decisions/M3.md`, "Réponse de l'auteur"."""

import errno
import os
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import structlog
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import plan_resource
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.rename_plan import (
    ItemMove,
    PlannedRename,
    RenameBook,
    RenameConfirmationRequiredError,
    RenameRefusedError,
    _fold,
    validate_rel_path,
)
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.local_socket import Peer
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.interfaces.local_cli.ops import run_ops_method
from shelfhold.interfaces.local_cli.principal import principal_for_peer
from shelfhold.ops import CodedRefusal
from tests.export_env import ExportEnv, export_env
from tests.ops_env import job_status, make_ops
from tests.rename_env import (
    FakeReaders,
    export_chapters,
    item_ids_of,
    open_intents,
    plan_status,
    record_paths,
    resume_plan,
    tree,
)


@pytest.fixture
def env() -> Iterator[ExportEnv]:
    with export_env() as made:
        yield made


def _apply(env: ExportEnv, preview: Any, *, confirm: bool = False, job: str | None = None) -> None:
    env.pipeline.apply(
        preview.plan_id, if_match=preview.base_version, job_id=job or new_ulid(),
        confirm_progress_loss=confirm,
    )  # fmt: skip


def _series_moves(env: ExportEnv, folder: str, new_folder: str) -> list[ItemMove]:
    return [ItemMove(i, f"{new_folder}/{n}") for n, i in item_ids_of(env, folder).items()]


def _op_codes(env: ExportEnv, plan_id: str) -> list[tuple[str, str | None]]:
    table = coherence.export_op
    with env.engine.connect() as conn:
        rows = conn.execute(
            sa.select(table.c.status, table.c.error_code)
            .where(table.c.plan_id == plan_id)
            .order_by(table.c.batch, table.c.position)
        ).all()
    return [(r.status, r.error_code) for r in rows]


# -- 1. a chain of renames: ordered, or given back ---------------------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_a_chain_is_ordered_and_a_swap_goes_through_a_temporary_name(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1", "2", "3"])
    ids = item_ids_of(env, "Dr Stone")
    one, two, three = (ids[f"Dr Stone - Ch.{n}.cbz"] for n in "123")
    inode = {n: (env.root / "Dr Stone" / f"Dr Stone - Ch.{n}.cbz").stat().st_ino for n in "123"}
    swap = [  # Ch.1 and Ch.2 trade their names, Ch.3 goes to the old name of Ch.1... in a ring
        ItemMove(one, "Dr Stone/Dr Stone - Ch.2.cbz"),
        ItemMove(two, "Dr Stone/Dr Stone - Ch.3.cbz"),
        ItemMove(three, "Dr Stone/Dr Stone - Ch.1.cbz"),
    ]
    preview = env.pipeline.rename_preview(env.destination_id, moves=swap)
    assert any(s.dst.endswith(".shelfhold-tmp") for s in preview.steps)  # the ring is broken once
    env.scans.clear()
    _apply(env, preview)
    names = [f"Dr Stone/Dr Stone - Ch.{n}.cbz" for n in "123"]
    assert tree(env) == names and record_paths(env) == names
    moved = {n: (env.root / "Dr Stone" / f"Dr Stone - Ch.{n}.cbz").stat().st_ino for n in "123"}
    assert moved == {"2": inode["1"], "3": inode["2"], "1": inode["3"]}  # the same files, renamed
    assert open_intents(env) == 0 and plan_status(env, preview.plan_id or "") == "done"
    assert len(env.scans) == 1


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_a_step_the_disk_refuses_gives_the_batch_back_and_blocks_nothing(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_chapters(env, "Dr Stone", ["1", "2", "3"])
    before_tree, before_records = tree(env), record_paths(env)
    ids = item_ids_of(env, "Dr Stone")
    moves = [ItemMove(ids[f"Dr Stone - Ch.{n}.cbz"], f"Dr Stone/Vol {n}.cbz") for n in "123"]
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    real = env.lib.fsops.rename
    calls = {"n": 0}

    def third_fails(src: str, dst: str, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(errno.EACCES, "refused")
        real(src, dst, **kwargs)

    monkeypatch.setattr(env.lib.fsops, "rename", third_fails)
    with pytest.raises(RenameRefusedError) as refused:
        _apply(env, preview)
    assert refused.value.reason == "refused_by_the_disk"
    monkeypatch.setattr(env.lib.fsops, "rename", real)
    assert tree(env) == before_tree and record_paths(env) == before_records  # given back
    assert open_intents(env) == 0 and plan_status(env, preview.plan_id or "") == "failed"
    statuses = _op_codes(env, preview.plan_id or "")
    assert statuses == [("skipped", None), ("skipped", None), ("failed", "refused_by_the_disk")]
    again = env.pipeline.rename_preview(env.destination_id, moves=moves)  # the series is free
    assert again.plan_id is not None


# -- 2. I9 in both directions, at the preview and at the apply -----------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_an_intent_opened_after_the_preview_stops_the_step_in_both_directions(
    env: ExportEnv,
) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    journal = IntentJournal(env.engine, env.lib.fsops)
    for opened, label in (
        (str(env.root / "Dr Stone" / "Dr Stone - Ch.9.cbz"), "under the folder"),
        (str(env.root / "Dr Stone"), "over the folder"),
    ):
        preview = env.pipeline.rename_preview(
            env.destination_id, moves=_series_moves(env, "Dr Stone", "Dr Stone (2019)")
        )
        intent = journal.record(op="write", src=opened + ".part", dst=opened)
        with pytest.raises(RenameRefusedError) as refused:
            _apply(env, preview)
        assert refused.value.reason == "intent_open", label
        assert (env.root / "Dr Stone" / "Dr Stone - Ch.1.cbz").is_file()  # nothing was moved
        journal.cancel(intent)


# -- 3. a key cannot replay the application of a rename plan -------------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-037", "EF-USR-024")
def test_retry_of_an_apply_job_asks_for_a_session_and_other_jobs_are_not_affected(
    env: ExportEnv,
) -> None:
    from shelfhold.runtime_jobs import APPLY_JOB_KIND, apply_key

    export_chapters(env, "Dr Stone", ["1"])
    rename = env.pipeline.rename_preview(
        env.destination_id, moves=_series_moves(env, "Dr Stone", "Dr Stone (2019)")
    )
    ops = make_ops(env)
    plain, dead = new_ulid(), new_ulid()
    for job_id, key in ((dead, apply_key(rename.plan_id or "", rename.base_version)),
                        (plain, "other.kind:x")):  # fmt: skip
        with env.engine.begin() as conn:
            conn.execute(
                sa.insert(platform.job_view).values(
                    id=job_id, kind=APPLY_JOB_KIND if job_id == dead else "other.kind",
                    status="dead_letter", dedupe_key=key, error_code="job.failed",
                    last_error="OSError", attempts=5, huey_task_id=new_ulid(),
                )
            )  # fmt: skip
    assert ops.jobs_retry_is_reserved(dead) and not ops.jobs_retry_is_reserved(plain)
    api_key = Principal("u", "u", "admin", Credential.API_KEY)
    for who in (None, api_key):  # nobody, or a key: refused, the job stays dead
        with pytest.raises(CodedRefusal):
            ops.jobs_retry(dead, principal=who)
        assert job_status(env.engine, dead) == "dead_letter"
    host = principal_for_peer(Peer(os.getpid(), os.getuid(), os.getgid()))
    assert run_ops_method(ops, host, "ops.jobs_retry", {"job_id": dead}) == {"ok": True}
    assert job_status(env.engine, dead) != "dead_letter"  # the host's admin, as a session, may
    ops.jobs_retry(plain)  # a job of another kind goes back as before
    assert job_status(env.engine, plain) != "dead_letter"


# -- 4. what the disk refuses closes its intent --------------------------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
@pytest.mark.parametrize("code", [errno.EXDEV, errno.EBUSY, errno.EACCES])
def test_a_file_rename_the_disk_refuses_leaves_nothing_for_the_boot_to_replay(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    ids = item_ids_of(env, "Dr Stone")
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=[ItemMove(ids["Dr Stone - Ch.1.cbz"], "Dr Stone/One.cbz")]
    )
    real = env.lib.fsops.rename

    def refuse(src: str, dst: str, **kwargs: Any) -> None:
        raise OSError(code, "refused by the disk")

    monkeypatch.setattr(env.lib.fsops, "rename", refuse)
    with pytest.raises(RenameRefusedError) as refused:
        _apply(env, preview)
    assert refused.value.reason == "refused_by_the_disk"
    monkeypatch.setattr(env.lib.fsops, "rename", real)
    assert open_intents(env) == 0
    report = IntentJournal(env.engine, env.lib.fsops).resolve_open()  # the next start
    assert not report.replayed and not report.completed  # no loop on the same failing rename
    assert (env.root / "Dr Stone" / "Dr Stone - Ch.1.cbz").is_file()


# -- 5. one application of a plan at a time --------------------------------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-042")
def test_the_request_of_a_second_application_is_refused_while_a_job_holds_the_plan(
    env: ExportEnv,
) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=_series_moves(env, "Dr Stone", "Dr Stone (2019)")
    )
    plan_id = preview.plan_id or ""
    env.pipeline.require_not_applying(plan_id)  # nobody on it: the request goes on
    locks = env.pipeline._locks
    with locks.hold_resources(new_ulid(), [plan_resource(plan_id)]):
        with pytest.raises(StalePlanError) as busy:
            env.pipeline.require_not_applying(plan_id)
        assert busy.value.code == "plan.applying" and busy.value.status == 409
    env.pipeline.require_not_applying(plan_id)  # the lock is released with the job
    assert plan_status(env, plan_id) == "planned"  # the refusals touched nothing


# -- 6. the names of an export --------------------------------------------------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-045")
def test_a_reshelving_names_like_an_export_and_collides_like_kavita() -> None:
    nfd = unicodedata.normalize("NFD", "Café")
    assert _fold(nfd) == _fold("café") == _fold("CAFÉ ")  # normalisation and case: one name
    assert validate_rel_path("Café/x.cbz", export_names=True)
    for bad in (f"{nfd}/x.cbz", "Dr Stone./x.cbz", "Dr: Stone/x.cbz", "A/ b.cbz"):
        with pytest.raises(RenameRefusedError) as refused:
            validate_rel_path(bad, export_names=True)
        assert refused.value.reason == "invalid_path", bad


@pytest.mark.req("EF-EXP-030", "EF-EXP-045")
def test_a_name_that_differs_by_case_only_is_taken(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    (env.root / "Cafe").mkdir()
    with pytest.raises(RenameRefusedError) as refused:
        env.pipeline.rename_preview(
            env.destination_id, moves=_series_moves(env, "Dr Stone", "CAFE")
        )
    assert refused.value.reason == "destination_taken"
    ids = item_ids_of(env, "Dr Stone")
    (env.root / "Dr Stone" / "Other.cbz").write_bytes(b"x")
    with pytest.raises(RenameRefusedError) as taken:  # a file of the same folder, other case
        env.pipeline.rename_preview(
            env.destination_id, moves=[ItemMove(ids["Dr Stone - Ch.1.cbz"], "Dr Stone/OTHER.cbz")]
        )
    assert taken.value.reason == "destination_taken"


# -- 7. a confirmation is for the count it was given for -------------------------------------------


@pytest.mark.req("EF-EXP-037")
def test_the_confirmation_covers_the_announced_count_and_no_more(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    readers = FakeReaders(count=3)
    env.pipeline.bind_readers(readers)
    moves = _series_moves(env, "Dr Stone", "Dr Stone (2019)")
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    plan_id = preview.plan_id or ""
    with pytest.raises(RenameConfirmationRequiredError):  # not confirmed
        env.pipeline.require_confirmation(plan_id, confirmed=False)
    env.pipeline.require_confirmation(plan_id, confirmed=True)  # the count shown: accepted
    readers.count = 2
    env.pipeline.require_confirmation(plan_id, confirmed=True)  # fewer readers: still covered
    readers.count = 4
    with pytest.raises(RenameConfirmationRequiredError) as more:  # more: a new confirmation
        env.pipeline.require_confirmation(plan_id, confirmed=True)
    assert more.value.readers == 4
    readers.count, readers.exact = 3, False  # exact when shown, a floor now
    with pytest.raises(RenameConfirmationRequiredError):
        env.pipeline.require_confirmation(plan_id, confirmed=True)


@pytest.mark.req("EF-EXP-037")
def test_the_job_relies_on_the_confirmation_its_request_recorded(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    env.pipeline.bind_readers(FakeReaders(count=3))
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=_series_moves(env, "Dr Stone", "Dr Stone (2019)")
    )
    with pytest.raises(RenameConfirmationRequiredError):  # the job alone confirms nothing
        _apply(env, preview)
    env.pipeline.require_confirmation(preview.plan_id or "", confirmed=True)  # the request
    _apply(env, preview)  # the job, with no flag of its own
    assert plan_status(env, preview.plan_id or "") == "done"


# -- 8. no count in a log --------------------------------------------------------------------------


@pytest.mark.req("EF-EXP-037")
def test_the_count_is_in_the_answer_and_in_no_log_line(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    env.pipeline.bind_readers(FakeReaders(count=7351))
    with structlog.testing.capture_logs() as logs:
        preview = env.pipeline.rename_preview(
            env.destination_id, moves=_series_moves(env, "Dr Stone", "Dr Stone (2019)")
        )
        with pytest.raises(RenameConfirmationRequiredError) as refused:
            _apply(env, preview)
        _apply(env, preview, confirm=True)
    assert preview.warnings[0].readers == 7351 and refused.value.readers == 7351
    assert "7351" not in str(logs) and "7351" not in str(refused.value)
    assert str(refused.value) == "confirmation_required"


# -- 9. one scan per batch, and a rescan of the batches done on the resume -------------------------


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_each_batch_drains_its_scan_and_the_resume_scans_the_batches_done(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    export_chapters(env, "Blue Box", ["1"])
    events: list[str] = []
    book = env.pipeline._renames
    book._request_scan = lambda folder: events.append(f"scan {Path(folder).name}")
    env.pipeline.bind_scan_drain(lambda: events.append("drain"))
    moves = _series_moves(env, "Dr Stone", "Dr Stone (2019)") + _series_moves(
        env, "Blue Box", "Blue Box (2021)"
    )
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    _apply(env, preview)
    assert [e for e in events if e == "drain"] == ["drain", "drain"]  # one per batch
    assert events[1] == "drain" and events[-1] == "drain"  # the scan of a batch is sent at once
    assert sorted(e for e in events if e != "drain") == [
        "scan Blue Box (2021)", "scan Dr Stone (2019)"
    ]  # fmt: skip
    # a stop after the first batch: the plan is `applying`, the second batch not marked done
    with env.engine.begin() as conn:
        conn.execute(sa.update(coherence.export_plan).values(status="applying"))
        conn.execute(
            sa.update(coherence.export_op).where(coherence.export_op.c.batch == 2)
            .values(status="pending")
        )  # fmt: skip
    events.clear()
    resume_plan(env, preview.plan_id or "")
    scanned = {e for e in events if e != "drain"}
    assert scanned == {"scan Blue Box (2021)", "scan Dr Stone (2019)"}  # the batch done as well


# -- 10. any permutation of names ends, in bounded time, without a file replaced -------------------


class _NoDisk:
    """The disk of `_ordered`: nothing exists, so that no temporary name is ever taken."""

    def observe(self, local: str) -> None:
        return None


class _Ctx:
    @staticmethod
    def local(rel: str) -> str:
        return rel


@st.composite
def _renames(draw: st.DrawFn) -> list[PlannedRename]:
    """Injective renames among `n` files and a few free names: chains, swaps, several cycles."""
    count = draw(st.integers(min_value=1, max_value=14))
    pool = [f"d/{i}.cbz" for i in range(count + 3)]
    sources = draw(st.lists(st.sampled_from(pool[:count]), unique=True, min_size=1))
    targets = draw(st.permutations(pool))[: len(sources)]
    return [
        PlannedRename("file", src, dst, f"ITEM{n:04d}")
        for n, (src, dst) in enumerate(zip(sources, targets, strict=True))
        if src != dst
    ]


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
@settings(max_examples=400, deadline=2000, suppress_health_check=[HealthCheck.too_slow])
@given(steps=_renames())
def test_every_permutation_is_ordered_in_bounded_time_without_replacing_a_file(
    steps: list[PlannedRename],
) -> None:
    book = object.__new__(RenameBook)
    book._fsops = _NoDisk()  # type: ignore[assignment]
    ordered = book._ordered(_Ctx(), steps)  # type: ignore[arg-type]
    assert len(ordered) <= 2 * len(steps)  # at most one temporary name per cycle
    disk = {s.src for s in steps}  # what exists; the targets are free (or vacated by a step)
    free_at_start = {s.dst for s in steps} - disk
    taken = set(disk)
    for step in ordered:
        assert step.src in taken and step.dst not in taken, (step, ordered)  # nothing replaced
        taken.remove(step.src)
        taken.add(step.dst)
    assert taken == {s.dst for s in steps} | (disk - {s.src for s in steps})  # the plan, exactly
    assert free_at_start <= taken and not any(".shelfhold-tmp" in t for t in taken)
    seen = [(s.src, s.dst) for s in ordered]
    assert len(seen) == len(set(seen))  # no step twice


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_two_swaps_and_a_ring_in_one_batch_apply_fully(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1", "2", "3", "4", "5", "6", "7"])
    ids = item_ids_of(env, "Dr Stone")
    name = "Dr Stone - Ch.{}.cbz".format
    target = {1: 2, 2: 1, 3: 4, 4: 3, 5: 6, 6: 7, 7: 5}  # two swaps and a ring of three
    moves = [ItemMove(ids[name(a)], f"Dr Stone/{name(b)}") for a, b in target.items()]
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    _apply(env, preview)
    assert tree(env) == [f"Dr Stone/{name(n)}" for n in range(1, 8)]
    assert record_paths(env) == tree(env) and open_intents(env) == 0
    assert plan_status(env, preview.plan_id or "") == "done"


# -- 11. readers that appear after a silent preview can be confirmed -------------------------------


@pytest.mark.req("EF-EXP-037")
def test_the_count_a_refusal_tells_is_the_one_the_next_confirmation_covers(
    env: ExportEnv,
) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    readers = FakeReaders(count=0)
    env.pipeline.bind_readers(readers)
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=_series_moves(env, "Dr Stone", "Dr Stone (2019)")
    )
    plan_id = preview.plan_id or ""
    assert preview.warnings == ()
    readers.count = 2
    with pytest.raises(RenameConfirmationRequiredError) as told:
        env.pipeline.require_confirmation(plan_id, confirmed=True)  # a flag sent blind: refused
    assert told.value.readers == 2
    env.pipeline.require_confirmation(plan_id, confirmed=True)  # the count it told: covered
    readers.count = 3  # and one more reader is not
    with pytest.raises(RenameConfirmationRequiredError):
        env.pipeline.require_confirmation(plan_id, confirmed=True)


# -- minor: the emptied folders go by an intent, a batch given back still scans and cleans -------


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_a_folder_the_batch_empties_goes_by_a_rmdir_intent(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_chapters(env, "Dr Stone", ["1", "2"])
    kinds: list[str] = []
    real = IntentJournal.record

    def spy(self: IntentJournal, **kwargs: Any) -> str:
        kinds.append(str(kwargs.get("op")))
        return real(self, **kwargs)

    monkeypatch.setattr(IntentJournal, "record", spy)
    ids = item_ids_of(env, "Dr Stone")
    moves = [  # two folders: not one whole move of the series folder
        ItemMove(ids["Dr Stone - Ch.1.cbz"], "Alpha/One.cbz"),
        ItemMove(ids["Dr Stone - Ch.2.cbz"], "Beta/Two.cbz"),
    ]
    _apply(env, env.pipeline.rename_preview(env.destination_id, moves=moves))
    assert not (env.root / "Dr Stone").exists() and "rmdir" in kinds
    assert open_intents(env) == 0


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_a_batch_given_back_still_asks_for_its_scan_and_drops_the_folder_it_made(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_chapters(env, "Dr Stone", ["1", "2"])
    ids = item_ids_of(env, "Dr Stone")
    first, second = sorted(ids)
    moves = [ItemMove(ids[first], "Alpha/One.cbz"), ItemMove(ids[second], "Beta/Two.cbz")]
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    real = env.lib.fsops.rename
    calls = {"n": 0}

    def second_fails(src: str, dst: str, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.EACCES, "refused")
        real(src, dst, **kwargs)

    monkeypatch.setattr(env.lib.fsops, "rename", second_fails)
    env.scans.clear()
    with pytest.raises(RenameRefusedError):
        _apply(env, preview)
    monkeypatch.setattr(env.lib.fsops, "rename", real)
    assert tree(env) == [f"Dr Stone/{n}" for n in sorted(ids)]  # given back
    assert not (env.root / "Alpha").exists()  # the folder made for the step is gone
    assert env.scans  # Kavita is asked to look again
    assert open_intents(env) == 0
