"""Rename plans for a template change, a reshelving and a binding migration (M3 L29, EF-EXP-030,
EF-EXP-037, EF-EXP-041): never a deletion then a recreation, batches with one scan each, the
atomic move of a series folder, the warning with the number of readers, and the attacks on the
disk (a name taken, a link planted during the move, a folder outside the managed root, a plan
applied twice, a step cut short at every stage)."""

import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.rename_plan import (
    ItemMove,
    RenameConfirmationRequiredError,
    RenameRefusedError,
)
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE, NamingTemplate
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export
from tests.export_env import ExportEnv, export_env
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

LONGER = replace(DEFAULT_TEMPLATE, template_id="kavita-manga", version=2,
                 chapter="{series} - Chapter {number}")  # fmt: skip


@pytest.fixture
def env() -> Iterator[ExportEnv]:
    with export_env() as made:
        yield made


class Spy:
    """The mutating calls of `fsops`, in order: what the plan did to the disk, and nothing else."""

    MUTATING = ("rename", "write_atomic", "recycle", "unlink", "rmdir", "mkdir", "touch_mtime")

    def __init__(self, env: ExportEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        for name in self.MUTATING:
            real = getattr(env.lib.fsops, name, None)
            if real is not None:
                monkeypatch.setattr(env.lib.fsops, name, self._wrap(name, real))

    def _wrap(self, name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args))
            return real(*args, **kwargs)

        return call

    def named(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.calls if called == name]


def _apply(env: ExportEnv, preview: Any, *, confirm: bool = False) -> None:
    env.pipeline.apply(
        preview.plan_id, if_match=preview.base_version, job_id=new_ulid(),
        confirm_progress_loss=confirm,
    )  # fmt: skip


def _move_series(env: ExportEnv, folder: str, new_folder: str) -> list[ItemMove]:
    return [
        ItemMove(item_id, f"{new_folder}/{name}")
        for name, item_id in item_ids_of(env, folder).items()
    ]


def _inodes(env: ExportEnv) -> dict[str, int]:
    return {p.name: p.stat().st_ino for p in env.files()}


@pytest.mark.req("EF-EXP-030")
def test_template_change_is_rename_plan_not_recreate(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_chapters(env, "Dr Stone", ["1", "2", "3"])
    before = _inodes(env)
    spy = Spy(env, monkeypatch)
    preview = env.pipeline.rename_preview(env.destination_id, template=LONGER)
    assert {s.kind for s in preview.steps} == {"file"}
    assert sorted(s.dst for s in preview.steps) == [
        "Dr Stone/Dr Stone - Chapter 1.cbz",
        "Dr Stone/Dr Stone - Chapter 2.cbz",
        "Dr Stone/Dr Stone - Chapter 3.cbz",
    ]
    with env.engine.connect() as conn:  # the stored plan says what the preview says: renames only
        kinds = set(
            conn.execute(
                sa.select(coherence.export_op.c.op).where(
                    coherence.export_op.c.plan_id == preview.plan_id
                )
            ).scalars()
        )
        plan_kind = conn.execute(
            sa.select(coherence.export_plan.c.kind).where(
                coherence.export_plan.c.plan_id == preview.plan_id
            )
        ).scalar_one()
    assert kinds == {"rename"} and plan_kind == "rename_plan"
    assert tree(env) == [
        f"Dr Stone/Dr Stone - Ch.{n}.cbz" for n in "123"
    ]  # a dry run moves nothing
    _apply(env, preview)
    assert tree(env) == [f"Dr Stone/Dr Stone - Chapter {n}.cbz" for n in "123"]
    assert sorted(_inodes(env).values()) == sorted(before.values())  # the same files, renamed
    assert record_paths(env) == tree(env)
    assert spy.named("recycle") == spy.named("unlink") == spy.named("write_atomic") == []
    with env.engine.connect() as conn:
        refs = set(conn.execute(sa.select(export.export_record.c.template_ref)).scalars())
        current = conn.execute(sa.select(export.shelf_destination.c.template)).scalar_one()
    assert refs == {LONGER.ref} and NamingTemplate.loads(current) == LONGER
    assert plan_status(env, preview.plan_id) == "done"
    # the next export names with the new template: nothing is written a second time
    assert env.pipeline.run(env.destination_id, job_id=new_ulid()).ops == ()


@pytest.mark.req("EF-EXP-037", "EF-EXP-030")
def test_rename_before_m4_warns_progress_loss_with_reader_count(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1", "2"])
    readers = FakeReaders(count=2)
    env.pipeline.bind_readers(readers)
    preview = env.pipeline.rename_preview(env.destination_id, template=LONGER)
    (warning,) = preview.warnings
    assert warning.code == "progress_loss" and warning.readers == 2 and warning.exact is True
    assert len(readers.asked) == 1 and len(readers.asked[0]) == 1  # the folders the plan touches
    assert readers.asked[0][0].endswith("/Dr Stone")  # as Kavita sees them, nothing else
    before = tree(env)
    with pytest.raises(RenameConfirmationRequiredError) as refused:
        _apply(env, preview)  # nothing is applied without the confirmation
    assert refused.value.readers == 2 and refused.value.status == 409
    assert tree(env) == before and plan_status(env, preview.plan_id) == "planned"
    _apply(env, preview, confirm=True)
    assert tree(env) == [f"Dr Stone/Dr Stone - Chapter {n}.cbz" for n in "12"]


@pytest.mark.req("EF-EXP-037")
def test_unknown_reader_count_needs_the_confirmation_too(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    env.pipeline.bind_readers(FakeReaders(count=0, exact=False))  # Kavita could not be asked
    preview = env.pipeline.rename_preview(env.destination_id, template=LONGER)
    (warning,) = preview.warnings
    assert warning.exact is False
    with pytest.raises(RenameConfirmationRequiredError):
        _apply(env, preview)  # fails closed: an unreadable count is not "nobody reads it"


@pytest.mark.req("EF-EXP-037")
def test_plan_without_path_change_applies_without_warning(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    readers = FakeReaders(count=5)
    env.pipeline.bind_readers(readers)
    same = env.pipeline.rename_preview(env.destination_id, template=DEFAULT_TEMPLATE)
    assert same.steps == () and same.warnings == () and same.plan_id is None
    series = env.main_series()
    env.add_chapter(series, "2")  # an ordinary plan: one file arrives, nothing is renamed
    preview = env.pipeline.preview(env.destination_id)
    assert not [w for w in preview.warnings if getattr(w, "code", "") == "progress_loss"]
    env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id=new_ulid())
    assert readers.asked == []  # readers are asked only when a path or a name would change


@pytest.mark.req("EF-EXP-030")
def test_rename_batch_moves_series_dir_atomically(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_chapters(env, "Dr Stone", ["1", "2", "3"])
    export_chapters(env, "Blue Box", ["1", "2"])
    with env.engine.begin() as conn:  # an adopted series keeps the folder it was found in
        series = export.export_series
        conn.execute(
            sa.update(series).values(series_dir=sa.case(
                (series.c.name_raw == "Dr Stone", "Dr Stone"), else_="Blue Box"))
        )  # fmt: skip
    before = _inodes(env)
    spy = Spy(env, monkeypatch)
    moves = _move_series(env, "Dr Stone", "Dr Stone (2019)") + _move_series(
        env, "Blue Box", "Blue Box (2021)"
    )
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    assert preview.batches == 2  # one batch per series, a consistent state after each
    assert [s.kind for s in preview.steps] == ["series_dir", "series_dir"]  # not file by file
    env.scans.clear()
    _apply(env, preview)
    renames = [(str(src), str(dst)) for src, dst, *_ in spy.named("rename")]
    assert len(renames) == 2 and all(Path(s).is_absolute() for s, _ in renames)
    assert {Path(s).name for s, _ in renames} == {"Dr Stone", "Blue Box"}  # the folders, not files
    assert tree(env) == sorted(
        [f"Dr Stone (2019)/Dr Stone - Ch.{n}.cbz" for n in "123"]
        + [f"Blue Box (2021)/Blue Box - Ch.{n}.cbz" for n in "12"]
    )
    assert not (env.root / "Dr Stone").exists() and not (env.root / "Blue Box").exists()
    assert sorted(_inodes(env).values()) == sorted(before.values())
    assert record_paths(env) == tree(env)
    assert len(env.scans) == 2  # one targeted scan per batch (EF-EXP-030)
    with env.engine.connect() as conn:
        dirs = set(conn.execute(sa.select(export.export_series.c.series_dir)).scalars())
    assert dirs == {"Dr Stone (2019)", "Blue Box (2021)"}  # the series folder follows its files


@pytest.mark.req("EF-EXP-030")
def test_rename_cleans_the_managed_folder_it_empties_and_only_that(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1", "2"])
    (env.root / "Dr Stone" / "Extras").mkdir()
    one = next(iter(item_ids_of(env, "Dr Stone").items()))
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=[ItemMove(one[1], f"Dr Stone/Extras/{one[0]}")]
    )
    _apply(env, preview)
    assert (env.root / "Dr Stone" / "Extras" / one[0]).is_file()
    assert (env.root / "Dr Stone").is_dir()  # still holds a chapter: kept
    emptied = item_ids_of(env, "Dr Stone")
    both = [ItemMove(i, f"Elsewhere/{Path(n).name}") for n, i in emptied.items()]
    second = env.pipeline.rename_preview(env.destination_id, moves=both)
    _apply(env, second)
    assert env.root.is_dir() and (env.root / "Elsewhere").is_dir()
    assert not (env.root / "Dr Stone").exists() or any((env.root / "Dr Stone").iterdir())


@pytest.mark.req("EF-EXP-041", "EF-EXP-030")
@pytest.mark.parametrize("crash", ["recorded", "applying", "before_rename", "after_rename", "op"])
def test_rename_interrupted_resumes_from_intent(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.export.application.plans import PlanStore

    class Killed(BaseException):
        """kill -9: nothing after the point runs, no handler settles anything."""

    export_chapters(env, "Dr Stone", ["1", "2"])
    export_chapters(env, "Blue Box", ["1"])
    before = _inodes(env)
    moves = _move_series(env, "Dr Stone", "Dr Stone (2019)") + _move_series(
        env, "Blue Box", "Blue Box (2021)"
    )
    preview = env.pipeline.rename_preview(env.destination_id, moves=moves)
    fsops = env.lib.fsops
    real_rename, real_record = fsops.rename, IntentJournal.record
    real_applying, real_set_op = IntentJournal.mark_applying, PlanStore._set_op
    armed = {"on": True}

    def die_once() -> None:
        if armed["on"]:
            armed["on"] = False
            raise Killed

    def record(self: Any, *a: Any, **k: Any) -> str:
        intent = real_record(self, *a, **k)
        if crash == "recorded":
            die_once()
        return intent

    def applying(self: Any, *a: Any, **k: Any) -> None:
        real_applying(self, *a, **k)
        if crash == "applying":
            die_once()

    def rename(src: str, dst: str, **k: Any) -> None:
        if crash == "before_rename":
            die_once()
        real_rename(src, dst, **k)
        if crash == "after_rename":
            die_once()

    def set_op(self: Any, op_id: str, status: str, *reason: Any) -> None:
        if crash == "op" and status == "done":
            die_once()
        real_set_op(self, op_id, status, *reason)

    monkeypatch.setattr(IntentJournal, "record", record)
    monkeypatch.setattr(IntentJournal, "mark_applying", applying)
    monkeypatch.setattr(fsops, "rename", rename)
    monkeypatch.setattr(PlanStore, "_set_op", set_op)
    with pytest.raises(Killed):
        _apply(env, preview)
    monkeypatch.undo()
    with env.engine.begin() as conn:  # a killed process runs no handler: the plan stays applying
        conn.execute(sa.update(coherence.export_plan).values(status="applying"))
        conn.execute(
            sa.update(coherence.export_op).where(coherence.export_op.c.status == "failed")
            .values(status="pending")
        )  # fmt: skip
    resumed = resume_plan(env, preview.plan_id)
    assert resumed.settled == (1 if crash != "op" else 0)  # the intent the kill left, replayed
    assert open_intents(env) == 0
    assert tree(env) == sorted(
        [f"Dr Stone (2019)/Dr Stone - Ch.{n}.cbz" for n in "12"]
        + ["Blue Box (2021)/Blue Box - Ch.1.cbz"]
    )
    assert record_paths(env) == tree(env)  # no duplicate, no loss, the manifest agrees
    assert sorted(_inodes(env).values()) == sorted(before.values())
    assert plan_status(env, preview.plan_id) == "done"
    with pytest.raises(StalePlanError):  # and it is not applied a second time
        resume_plan(env, preview.plan_id)


@pytest.mark.req("EF-EXP-030", "EF-EXP-045")
def test_destination_taken_is_refused_and_nothing_is_overwritten(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    foreign = env.root / "Dr Stone (2019)"
    foreign.mkdir()
    (foreign / "Dr Stone - Ch.1.cbz").write_bytes(b"somebody else's file")
    moves = _move_series(env, "Dr Stone", "Dr Stone (2019)")
    with pytest.raises(RenameRefusedError) as refused:  # seen in the preview, nothing stored
        env.pipeline.rename_preview(env.destination_id, moves=moves)
    assert refused.value.reason == "destination_taken"
    assert (foreign / "Dr Stone - Ch.1.cbz").read_bytes() == b"somebody else's file"
    assert (env.root / "Dr Stone" / "Dr Stone - Ch.1.cbz").is_file()


@pytest.mark.req("EF-EXP-030", "EF-EXP-045")
def test_destination_taken_after_the_preview_is_refused_at_apply(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=_move_series(env, "Dr Stone", "Dr Stone (2019)")
    )
    taken = env.root / "Dr Stone (2019)"
    taken.mkdir()
    (taken / "mine.txt").write_text("x")
    with pytest.raises((StalePlanError, RenameRefusedError)):
        _apply(env, preview)
    assert (taken / "mine.txt").read_text() == "x"
    assert (env.root / "Dr Stone" / "Dr Stone - Ch.1.cbz").is_file()
    assert open_intents(env) == 0 and record_paths(env) == ["Dr Stone/Dr Stone - Ch.1.cbz"]


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_symlink_planted_during_the_move_is_never_followed(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.export.application.intents import IntentJournal

    export_chapters(env, "Dr Stone", ["1"])
    outside = env.lib.manga.parent / "outside-decoy"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours")
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=_move_series(env, "Dr Stone", "Dr Stone (2019)")
    )
    real_applying = IntentJournal.mark_applying

    def plant(self: Any, *a: Any, **k: Any) -> None:  # between the intent and the move
        real_applying(self, *a, **k)
        aside = env.root / "aside"
        os.rename(env.root / "Dr Stone", aside)
        os.symlink(outside, env.root / "Dr Stone")

    monkeypatch.setattr(IntentJournal, "mark_applying", plant)
    with pytest.raises(RenameRefusedError) as refused:
        _apply(env, preview)
    assert refused.value.reason == "not_a_folder"
    assert (outside / "keep.txt").read_text() == "not ours" and [
        p.name for p in outside.iterdir()
    ] == ["keep.txt"]
    assert not (env.root / "Dr Stone (2019)").exists()  # the link was not carried anywhere
    assert open_intents(env) == 0  # the intent is closed, not left to be replayed on the link
    assert plan_status(env, preview.plan_id) == "failed"


@pytest.mark.req("EF-EXP-030", "ENF-SEC-003")
def test_destination_parent_that_is_a_link_is_refused(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    outside = env.lib.manga.parent / "outside-decoy"
    outside.mkdir()
    os.symlink(outside, env.root / "Shelf")
    item = next(iter(item_ids_of(env, "Dr Stone").values()))
    with pytest.raises(RenameRefusedError) as refused:
        env.pipeline.rename_preview(
            env.destination_id, moves=[ItemMove(item, "Shelf/Dr Stone - Ch.1.cbz")]
        )
    assert refused.value.reason == "not_a_folder"
    assert list(outside.iterdir()) == []


@pytest.mark.req("EF-EXP-030", "EF-EXP-001")
@pytest.mark.parametrize(
    "target",
    ["../escape/a.cbz", "/etc/a.cbz", "A/../../a.cbz", ".shelfhold/a.cbz", ".shelfhold/x/a.cbz",
     "", "A//a.cbz", "A/./a.cbz", "A\\a.cbz", "a\x00b/a.cbz"],
)  # fmt: skip
def test_series_folder_outside_the_managed_root_is_refused(env: ExportEnv, target: str) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    item = next(iter(item_ids_of(env, "Dr Stone").values()))
    before = tree(env)
    with pytest.raises(RenameRefusedError) as refused:
        env.pipeline.rename_preview(env.destination_id, moves=[ItemMove(item, target)])
    assert refused.value.reason in {"outside_root", "reserved", "invalid_path"}
    assert tree(env) == before
    with env.engine.connect() as conn:  # nothing was stored for a refused plan
        stored = sa.select(sa.func.count()).where(coherence.export_plan.c.kind == "rename_plan")
        assert conn.execute(stored).scalar() == 0


@pytest.mark.req("EF-EXP-030")
def test_an_item_the_destination_does_not_hold_is_refused(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    with pytest.raises(RenameRefusedError) as refused:
        env.pipeline.rename_preview(env.destination_id, moves=[ItemMove(new_ulid(), "A/a.cbz")])
    assert refused.value.reason == "unknown_item"


@pytest.mark.req("EF-EXP-030", "EF-EXP-045")
def test_two_items_to_one_name_are_refused(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1", "2"])
    first, second = item_ids_of(env, "Dr Stone").values()
    with pytest.raises(RenameRefusedError) as refused:
        env.pipeline.rename_preview(
            env.destination_id,
            moves=[ItemMove(first, "Dr Stone/same.cbz"), ItemMove(second, "Dr Stone/SAME.cbz")],
        )
    assert refused.value.reason == "destination_taken"  # case-folded, like Kavita


@pytest.mark.req("EF-EXP-041", "EF-EXP-030")
def test_a_path_under_an_open_intent_is_not_planned(env: ExportEnv) -> None:
    from shelfhold.export.application.intents import IntentJournal

    export_chapters(env, "Dr Stone", ["1"])
    src = str(env.root / "Dr Stone" / "Dr Stone - Ch.1.cbz")
    IntentJournal(env.engine, env.lib.fsops).record(op="rename", src=src, dst=src + ".x")
    with pytest.raises(RenameRefusedError) as refused:
        env.pipeline.rename_preview(env.destination_id, template=LONGER)
    assert refused.value.reason == "intent_open"  # I9: it is somebody's operation in flight


@pytest.mark.req("EF-EXP-030", "EF-EXP-042")
def test_plan_applied_twice_is_refused_the_second_time(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_chapters(env, "Dr Stone", ["1", "2"])
    preview = env.pipeline.rename_preview(env.destination_id, template=LONGER)
    _apply(env, preview)
    spy = Spy(env, monkeypatch)
    after = tree(env)
    with pytest.raises(StalePlanError):
        _apply(env, preview)
    assert spy.calls == [] and tree(env) == after  # no second move, whatever the disk shows


@pytest.mark.req("EF-EXP-030", "EF-EXP-042")
def test_a_plan_is_stale_when_a_file_changed_since_the_preview(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    preview = env.pipeline.rename_preview(env.destination_id, template=LONGER)
    path = env.root / "Dr Stone" / "Dr Stone - Ch.1.cbz"
    path.write_bytes(path.read_bytes() + b"x")  # somebody rewrote it in place
    with pytest.raises(StalePlanError):
        _apply(env, preview)
    assert path.is_file() and open_intents(env) == 0


@pytest.mark.req("EF-EXP-030", "EF-EXP-041")
def test_no_write_outside_the_steps_of_the_plan(
    env: ExportEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_chapters(env, "Dr Stone", ["1", "2"])
    preview = env.pipeline.rename_preview(
        env.destination_id,
        moves=_move_series(env, "Dr Stone", "Dr Stone (2019)"),
    )
    spy = Spy(env, monkeypatch)
    _apply(env, preview)
    declared = {(str(env.root / s.src), str(env.root / s.dst)) for s in preview.steps}
    assert {(str(a[0]), str(a[1])) for a in spy.named("rename")} == declared
    for name in ("recycle", "unlink", "write_atomic", "touch_mtime"):
        assert spy.named(name) == [], name
    for args in spy.named("mkdir") + spy.named("rmdir"):  # parents of a target, emptied folders
        assert str(args[0]).startswith(str(env.root))


@pytest.mark.req("EF-EXP-030")
def test_replicas_follow_the_files(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1"])
    preview = env.pipeline.rename_preview(
        env.destination_id, moves=_move_series(env, "Dr Stone", "Dr Stone (2019)")
    )
    _apply(env, preview)
    with env.engine.connect() as conn:
        paths = set(
            conn.execute(
                sa.text("SELECT rel_path FROM replica WHERE location = 'destination'")
            ).scalars()
        )
    assert paths == {"Dr Stone (2019)/Dr Stone - Ch.1.cbz"} or all(
        p.endswith("Dr Stone (2019)/Dr Stone - Ch.1.cbz") for p in paths
    )
