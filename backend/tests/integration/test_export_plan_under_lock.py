"""Two exports of the same destination (a redeploy, a second job) each compute their plan before
the resource lock: the second one replays a plan that the first has just applied (EF-EXP-042).
The plan is computed, or computed again and checked, under the lock."""

import threading
from collections.abc import Callable
from typing import Any

import pytest

import shelfhold.export.application.pipeline as pipeline_module
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env

pytestmark = pytest.mark.req("EF-EXP-042")

BARRIER_SECONDS = 20


def _race(env: Any, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Two `run()` of one destination, held together once each has its plan (the barrier sits
    between the calculation of the plan and the first write). Returns what each one gave back
    (a report or the exception it raised)."""
    barrier = threading.Barrier(2, timeout=BARRIER_SECONDS)  # both have their plan
    claimed = threading.Barrier(2, timeout=BARRIER_SECONDS)  # both hold their claim, none writes
    first_done = threading.Event()  # the first run is over: the second one may take its locks
    real: Callable[..., Any] = ExportPipeline._compute
    real_hold: Callable[..., Any] = ResourceLocks.hold
    local = threading.local()

    def compute(self: ExportPipeline, destination_id: str, *, disk: bool = True) -> Any:
        found = real(self, destination_id, disk=disk)
        if disk and not getattr(local, "met", False):  # the plan the run is about to store
            local.met = True
            barrier.wait()
        return found

    def hold(self: ResourceLocks, *args: Any, **kwargs: Any) -> Any:
        if not getattr(local, "held", False):  # the first write of the run: its plan is claimed
            local.held = True
            claimed.wait()
            if local.index == 1:  # no polling for the lock while the first one writes: SQLite
                assert first_done.wait(BARRIER_SECONDS)  # would refuse a deferred upgrade
        return real_hold(self, *args, **kwargs)

    monkeypatch.setattr(ExportPipeline, "_compute", compute)
    monkeypatch.setattr(ResourceLocks, "hold", hold)
    results: list[object] = [None, None]

    def one(index: int) -> None:
        local.index = index
        try:
            results[index] = env.pipeline.run(env.destination_id, job_id=new_ulid())
        except Exception as error:
            results[index] = error
        finally:
            if index == 0:
                first_done.set()

    threads = [threading.Thread(target=one, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    return results


def test_two_concurrent_exports_of_a_destination_write_each_unit_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.add_chapter(series, "3")
        results = _race(env, monkeypatch)
        errors = [r for r in results if isinstance(r, Exception)]
        assert errors == [], errors  # the late one ends cleanly: nothing to do, no error
        assert sorted(p.name for p in env.files()) == ["Dr Stone - Ch.2.cbz", "Dr Stone - Ch.3.cbz"]
        assert len(env.rows(export.export_record)) == 2  # no second line for the same Unit
        assert len(env.rows(export.export_item)) == 2  # and no second item
        written = [len(r.ops) for r in results if isinstance(r, pipeline_module.ApplyReport)]
        assert sorted(written) == [0, 2]  # one run wrote the two, the other wrote nothing


def _intent(path: str) -> Any:
    from shelfhold.export.application.intents import OpenIntent

    return OpenIntent(
        id=new_ulid(), op="rename", src=None, dst=path, expected_sha256=None,
        expected_file_id=None, state="pending", epoch=0, schema_version="1",
    )  # fmt: skip


def test_the_lock_of_an_intent_without_a_line_is_the_one_of_its_destination() -> None:
    """`intents resolve` of a rename in a series folder meets a job that exports there, and a
    removal or detachment of the destination meets the intent (L30 follow-up)."""
    from types import SimpleNamespace

    from shelfhold.export.application.locks import ResourceLocks, dest_resource, path_resource
    from shelfhold.ops.errors import StillWaitingError
    from shelfhold.ops.intents import IntentOps

    with export_env() as env:
        ops = IntentOps(SimpleNamespace(engine=env.engine, clock=env.clock))  # type: ignore[arg-type]
        locks = ResourceLocks(env.engine, clock=env.clock)
        inside = _intent(str(env.root / "Dr Stone" / "Dr Stone - Ch.2.cbz"))
        name = path_resource(f"{env.destination_id}/Dr Stone")
        assert ops._path_locks(inside) == [name]
        with (  # an export holding the series folder: the intent is not ours to settle
            locks.hold_resources(new_ulid(), [name]),
            pytest.raises(StillWaitingError),
            ops._held(inside),
        ):
            pass
        with (  # a removal or a detachment of the destination
            locks.hold_resources(new_ulid(), [dest_resource(env.destination_id)]),
            pytest.raises(StillWaitingError),
            ops._held(inside),
        ):
            pass
        with ops._held(inside):
            assert locks.is_held(name)  # and while it is settled, an export of the folder waits
        outside = _intent("/elsewhere/received/x.cbz")
        assert ops._path_locks(outside) == ["path:/elsewhere/received/x.cbz"]


def test_the_root_of_a_destination_is_held_as_the_destination() -> None:
    """A path that is the root itself (a rename of the root, a mirror file at its top) has no
    series folder: it takes `dest:<id>`, which a removal or a detachment holds too."""
    from types import SimpleNamespace

    from shelfhold.export.application.locks import dest_resource
    from shelfhold.ops.intents import IntentOps

    with export_env() as env:
        ops = IntentOps(SimpleNamespace(engine=env.engine, clock=env.clock))  # type: ignore[arg-type]
        assert ops._path_locks(_intent(str(env.root))) == [dest_resource(env.destination_id)]


def test_destinations_cannot_nest_so_one_destination_holds_a_path() -> None:
    """The deepest-root rule of `_path_locks` never has two candidates: the creation of a
    destination refuses a root inside, or around, the root of another writing one."""
    from shelfhold.export.domain.bindings import BindingFacts, TreeOverlapError, check_binding

    outer = BindingFacts("A", "s1", "t", "tree", "foyer", "/library/Kavita")
    inner = BindingFacts("B", "s2", "t", "tree", "foyer", "/library/Kavita/Manga")
    for new, other in ((inner, outer), (outer, inner)):
        with pytest.raises(TreeOverlapError):
            check_binding(new, [other], retention_mode="keep")


def test_three_stale_attempts_end_with_a_message_about_the_export_not_a_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shelfhold.export.application.plans import StalePlanError

    calls: list[int] = []

    def stale(self: ExportPipeline, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise StalePlanError("the preview changed: recompute it")

    monkeypatch.setattr(ExportPipeline, "apply", stale)
    with export_env() as env:
        env.add_chapter(env.new_series(), "2")
        with pytest.raises(StalePlanError, match="other exports") as raised:
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert "preview" not in str(raised.value)
        assert len(calls) == pipeline_module.RUN_ATTEMPTS
