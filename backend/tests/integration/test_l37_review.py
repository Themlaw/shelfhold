"""PR #59 review of L37: one regression test per finding (ENF-OPS-006, -007, -010, -011)."""

import contextlib
import gc
import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.instance import InstanceLock
from shelfhold.infrastructure.observability.disk_guard import (
    DiskGuard,
    DiskLowError,
    DiskThresholds,
    Usage,
)
from shelfhold.infrastructure.observability.disk_watch import DiskWatcher
from shelfhold.infrastructure.observability.shutdown import (
    GracefulShutdown,
    ShuttingDownError,
    WriteBarrier,
)
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import JobPools, boot_jobs
from tests.fakes_clock import ManualClock
from tests.fsops_env import make_env

GIB = 1024**3
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


class Space:
    def __init__(self, free: int = 50 * GIB) -> None:
        self.free = free

    def __call__(self, _path: Path) -> Usage:
        return Usage(total=100 * GIB, free=self.free)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "shelfhold.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def run_queue(pools: JobPools) -> None:
    huey = pools.huey("io")
    while (task := huey.dequeue()) is not None:
        huey.execute(task)


def row_of(engine: Engine, key: str) -> tuple[str, int, str | None]:
    j = platform.job_view
    with engine.connect() as conn:
        r = conn.execute(
            sa.select(j.c.status, j.c.attempts, j.c.error_code).where(j.c.dedupe_key == key)
        ).one()
    return (r.status, r.attempts, r.error_code)


def new_pools(tmp_path: Path, engine: Engine) -> JobPools:
    (tmp_path / "data").mkdir(exist_ok=True)
    return JobPools(tmp_path / "data", engine=engine, clock=ManualClock(NOW), seed="i")


def watcher_for(pools: JobPools, engine: Engine, space: Space, tmp_path: Path) -> DiskWatcher:
    guard = DiskGuard({"store": tmp_path}, usage=space)
    return DiskWatcher(guard, pools, engine=engine, bus=EventBus(pools.clock))


# -- 1. the gate measures by itself ------------------------------------------------------------


@pytest.mark.req("ENF-OPS-006")
def test_the_gate_measures_again_after_its_ttl_without_any_job(tmp_path: Path) -> None:
    space, now = Space(), [0.0]
    guard = DiskGuard({"library": tmp_path}, usage=space, ttl_s=5, monotonic=lambda: now[0])
    guard.require_space()  # first measure
    space.free = 0
    now[0] = 2.0
    guard.require_space()  # inside the TTL: the snapshot stands
    now[0] = 6.0
    with pytest.raises(DiskLowError):
        guard.require_space()  # no `ops.disk_check` ran: the write itself measured
    state = guard.evaluate()  # the watcher still learns of the transition, to notify it
    assert [c.low for c in state.changes] == [True]


# -- 2. restart ------------------------------------------------------------------------------


@pytest.mark.req("ENF-OPS-006")
def test_jobs_parked_by_a_previous_run_resume_at_boot_when_space_is_back(
    tmp_path: Path, engine: Engine
) -> None:
    pools = new_pools(tmp_path, engine)
    pools.register("download", pool="io", handler=lambda job: None)
    space = Space()
    pools.submit("download", dedupe_key="c1")
    space.free = 4 * GIB
    watcher_for(pools, engine, space, tmp_path).check()
    run_queue(pools)
    assert row_of(engine, "c1") == ("paused", 0, "disk.low")
    space.free = 40 * GIB  # freed while the server was down
    lock = InstanceLock(tmp_path / "data")
    lock.acquire()
    try:
        again = new_pools(tmp_path, engine)
        ran: list[str] = []
        again.register("download", pool="io", handler=lambda job: ran.append(job.dedupe_key))
        boot_jobs(again, instance_lock=lock, resolve_intents=lambda: None)
        watcher_for(again, engine, space, tmp_path).check()
        run_queue(again)
    finally:
        lock.release()
    assert ran == ["c1"] and row_of(engine, "c1") == ("succeeded", 1, None)


# -- 3. the disk fills during a job ------------------------------------------------------------


@pytest.mark.req("ENF-OPS-006", "ENF-OPS-011")
def test_a_job_that_meets_the_gate_is_parked_not_failed(tmp_path: Path, engine: Engine) -> None:
    pools = new_pools(tmp_path, engine)
    space = Space()
    watcher = watcher_for(pools, engine, space, tmp_path)
    attempts: list[int] = []

    def download(job: object) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            space.free = 4 * GIB
            watcher._guard.evaluate()
            watcher._guard.require_space()

    pools.register("download", pool="io", handler=download)
    pools.submit("download", dedupe_key="c1")
    run_queue(pools)
    assert row_of(engine, "c1") == ("paused", 0, "disk.low")  # no attempt kept, no job.failed
    space.free = 40 * GIB
    watcher.check()
    run_queue(pools)
    assert row_of(engine, "c1") == ("succeeded", 1, None)


# -- 4. SIGTERM must not eat a task ------------------------------------------------------------


@pytest.mark.req("ENF-OPS-007")
def test_a_task_reaching_a_worker_after_sigterm_runs_at_the_next_boot(
    tmp_path: Path, engine: Engine
) -> None:
    lock = InstanceLock(tmp_path / "data")
    (tmp_path / "data").mkdir()
    lock.acquire()
    pools = new_pools(tmp_path, engine)
    pools.register("later", pool="io", handler=lambda job: None)
    pools.submit("later", dedupe_key="l1")
    GracefulShutdown(pools, instance_lock=lock, checkpoint_s=0.1).run()
    run_queue(pools)  # the worker takes the task and gives it back: Huey forgets it
    assert pools.huey("io").pending_count() == 0
    second = InstanceLock(tmp_path / "data")
    second.acquire()
    try:
        ran: list[int] = []
        again = new_pools(tmp_path, engine)
        again.register("later", pool="io", handler=lambda job: ran.append(1))
        boot_jobs(again, instance_lock=second, resolve_intents=lambda: None)
        run_queue(again)
    finally:
        second.release()
    assert ran == [1] and row_of(engine, "l1") == ("succeeded", 1, None)


# -- 5. no descriptor grows with the application's life cycle -----------------------------------


def open_fds() -> int:
    gc.collect()
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.req("ENF-OPS-007")
def test_successive_apps_and_lifespans_do_not_grow_the_descriptors(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from shelfhold.infrastructure.migrate import migrate
    from shelfhold.main import create_app

    migrate(settings)

    def life() -> None:
        with TestClient(create_app(settings)) as client:
            assert client.get("/healthz").status_code == 200

    life()  # warm-up: imports, caches
    before = open_fds()
    for _ in range(5):
        life()
    assert open_fds() - before <= 2, "a lifespan leaves descriptors behind (Huey storage?)"


@pytest.mark.req("ENF-OPS-007")
def test_the_image_worker_reads_its_reply_with_selectors() -> None:
    source = (
        Path(__file__).parents[2] / "src/shelfhold/infrastructure/imaging/client.py"
    ).read_text()
    assert "select.select(" not in source and "selectors" in source


# -- 6. pause/resume race ----------------------------------------------------------------------


@pytest.mark.req("ENF-OPS-006")
def test_a_resume_between_the_read_and_the_park_does_not_strand_the_job(
    tmp_path: Path, engine: Engine
) -> None:
    pools = new_pools(tmp_path, engine)
    ran: list[int] = []
    pools.register("download", pool="io", handler=lambda job: ran.append(1))
    pools.submit("download", dedupe_key="c1")
    pools.pause_writes("disk.low")
    pools.resume_writes("disk.low")
    pools._paused_reason = "disk.low"
    real = pools.ledger.pause_one

    def late(job_id: str, code: str) -> bool:
        pools.resume_writes("disk.low")  # ...and the next one resumed before the park landed
        return real(job_id, code)

    pools.ledger.pause_one = late  # type: ignore[method-assign]
    run_queue(pools)
    pools.ledger.pause_one = real  # type: ignore[method-assign]
    run_queue(pools)
    assert row_of(engine, "c1")[0] == "succeeded" and ran == [1]


# -- 7. shutdown --------------------------------------------------------------------------------


@pytest.mark.req("ENF-OPS-007")
def test_shutdown_frees_everything_even_when_the_database_cannot_be_written(
    tmp_path: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "data").mkdir()
    lock = InstanceLock(tmp_path / "data")
    lock.acquire()
    pools = new_pools(tmp_path, engine)
    freed: list[str] = []

    def readonly() -> int:
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(pools.slots, "release_all", readonly)
    GracefulShutdown(
        pools,
        instance_lock=lock,
        release_resource_locks=lambda: freed.append("locks"),
        checkpoint_s=0.1,
    ).run()
    assert freed == ["locks"] and not lock.is_held()  # each release on its own


@pytest.mark.req("ENF-OPS-007")
def test_a_second_sigterm_is_a_no_op_even_inside_the_first_and_an_abandoned_job_cannot_write(
    tmp_path: Path, engine: Engine
) -> None:
    (tmp_path / "data").mkdir()
    lock = InstanceLock(tmp_path / "data")
    lock.acquire()
    pools = new_pools(tmp_path, engine)
    barrier = WriteBarrier()
    shutdown = GracefulShutdown(pools, instance_lock=lock, checkpoint_s=0.1, barrier=barrier)
    inner: list[object] = []
    real_wait = pools.wait_idle

    def reentrant(timeout: float) -> bool:
        inner.append(shutdown.run())  # SIGTERM again, in the middle of the first
        return real_wait(timeout)

    pools.wait_idle = reentrant  # type: ignore[method-assign]
    shutdown.run()
    assert len(inner) == 1 and not lock.is_held()
    made = make_env(tmp_path)
    fsops = Fsops.open(made.path_map, made.expected, stop_gate=barrier.require_open)
    fsops.boot()
    src = made.manga / "a.cbz"
    src.write_bytes(b"x")
    with pytest.raises(ShuttingDownError):  # the abandoned job goes on, the disk stays as is
        fsops.rename(str(src), str(made.manga / "b.cbz"))
    with pytest.raises(ShuttingDownError):
        fsops.write_atomic(str(made.manga / "c.cbz"), b"y")
    assert src.exists() and not (made.manga / "b.cbz").exists()


# -- 8. configuration and the guard ------------------------------------------------------------


@pytest.mark.req("ENF-OPS-006")
def test_resume_marks_must_be_strictly_above_pause_marks(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="strictly"):
        DiskThresholds(5, 10, 5, 10)
    with pytest.raises(ValueError, match="strictly"):
        Settings(
            data_dir=tmp_path / "d", store_dir=tmp_path / "s",
            disk_pause_percent=5, disk_resume_percent=5, disk_pause_gib=10, disk_resume_gib=10,
        )  # fmt: skip


@pytest.mark.req("ENF-OPS-006")
def test_the_mirror_write_goes_through_the_disk_gate(tmp_path: Path) -> None:
    made = make_env(tmp_path)
    space = Space(1 * GIB)
    guard = DiskGuard({"library": made.library}, usage=space)
    guard.evaluate()
    fsops = Fsops.open(made.path_map, made.expected, write_gate=guard.require_space)
    fsops.boot()
    with pytest.raises(DiskLowError):
        fsops.write_mirror(str(made.library), "series.jsonl", b"{}")


@pytest.mark.req("ENF-OPS-006")
def test_notifications_are_not_a_write_job() -> None:
    source = (Path(__file__).parents[2] / "src/shelfhold").rglob("*.py")
    for path in source:
        text = path.read_text(encoding="utf-8")
        if 'register("notify.dispatch"' in text:
            assert "writes=False" in text.split('register("notify.dispatch"', 1)[1].split(")", 1)[0]


# -- 9. the probe never takes a folder that is not its own -------------------------------------


@pytest.mark.req("EF-UX-002")
def test_the_probe_never_removes_a_folder_it_did_not_make(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.infrastructure.fsops import probe

    made = make_env(tmp_path)
    names = iter([".shelfhold-probe-" + c * 16 for c in "abc"])
    monkeypatch.setattr(probe, "new_probe_name", lambda: next(names))
    theirs = made.manga / (".shelfhold-probe-" + "c" * 16)
    theirs.mkdir()
    report = probe.probe_folder(made.fsops, str(made.manga))
    assert not report.ok and theirs.is_dir()  # refused, and theirs is still there


# -- 10. the watcher thread-safety of the pending changes is by construction -------------------


@pytest.mark.req("ENF-OPS-006")
def test_concurrent_gates_and_watcher_lose_no_transition(tmp_path: Path) -> None:
    space = Space(50 * GIB)
    guard = DiskGuard({"store": tmp_path}, usage=space, ttl_s=0)
    guard.evaluate()
    space.free = 1 * GIB
    threads = [threading.Thread(target=lambda: _swallow(guard)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [c.low for c in guard.evaluate().changes] == [True]  # announced once, not lost


def _swallow(guard: DiskGuard) -> None:
    with contextlib.suppress(DiskLowError):
        guard.require_space()
