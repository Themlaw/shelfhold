"""ENF-OPS-007: SIGTERM in three steps: stop claiming, checkpoint or abandon the operation in
flight (its intent stays open and replayable), then free the locks."""

import threading
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.infrastructure.instance import InstanceLock
from shelfhold.infrastructure.observability.shutdown import GracefulShutdown
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.jobs.huey_app import JobPools
from tests.intents_env import make_intent_env, sha

WORK = "01JBZ3X5QH8M2N4P6R7S9T0W01"


def job_rows(pools: JobPools) -> dict[str, tuple[str, int]]:
    j = platform.job_view
    with pools._engine.connect() as conn:
        return {r.dedupe_key: (r.status, r.attempts) for r in conn.execute(sa.select(j))}


@pytest.mark.req("ENF-OPS-007")
def test_graceful_shutdown_checkpoint_and_flush_locks(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    lock = InstanceLock(data)
    lock.acquire()
    pools = JobPools(data, engine=ienv.engine, clock=ienv.clock, seed="i")
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    pools.slots.declare("example.org", 1)
    journal = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock)
    src, dst = ienv.fs.manga / "a.cbz", ienv.fs.manga / "b.cbz"
    src.write_bytes(b"chapter")
    started, hang = threading.Event(), threading.Event()

    def stuck_rename(ctx: object) -> None:
        job_id = ctx.job_id  # type: ignore[attr-defined]
        with locks.hold(job_id, work=WORK, series="d/S", heartbeat=0.05):
            intent = journal.record(
                op="rename", src=str(src), dst=str(dst), expected_sha256=sha(b"chapter")
            )
            journal.mark_applying(intent)
            started.set()
            hang.wait(10)  # the worker never reaches the rename: the shutdown gives up on it

    pools.register("rename", pool="io", handler=stuck_rename)
    pools.register("later", pool="io", handler=lambda job: None)
    pools.submit("rename", dedupe_key="r1", limit_key="example.org")
    pools.submit("later", dedupe_key="l1")
    huey = pools.huey("io")
    worker = threading.Thread(target=lambda: huey.execute(huey.dequeue()), daemon=True)
    worker.start()
    assert started.wait(5)

    report = GracefulShutdown(
        pools, instance_lock=lock, release_resource_locks=locks.release_all, checkpoint_s=0.3
    ).run()
    assert report.abandoned == 1 and report.finished == 0  # step 2: given up after the delay
    huey.execute(huey.dequeue())  # step 1: a task that reaches a worker now is not claimed
    assert job_rows(pools)["l1"] == ("queued", 0)
    assert job_rows(pools)["r1"][0] == "running"  # left for `requeue_running` at the next boot
    with ienv.engine.connect() as conn:  # step 3: nothing is left locked
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(coherence.resource_lock)
            ).scalar_one()
            == 0
        )
        assert conn.execute(sa.select(sa.func.sum(platform.domain_slot.c.in_use))).scalar_one() == 0
    assert not lock.is_held()
    InstanceLock(data).acquire()  # the next instance can start at once

    intents = ienv.engine.connect().execute(sa.select(coherence.fs_intent.c.state)).scalars().all()
    assert intents == ["applying"]  # still open
    assert src.exists() and not dst.exists()
    settled = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).resolve_open()
    assert len(settled.replayed) == 1 and dst.read_bytes() == b"chapter"  # replayable
    assert len(pools.ledger.requeue_running()) == 1
    hang.set()
    worker.join(5)


@pytest.mark.req("ENF-OPS-007")
def test_a_cooperative_job_stops_at_its_checkpoint_and_resumes_there(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    lock = InstanceLock(data)
    lock.acquire()
    pools = JobPools(data, engine=ienv.engine, clock=ienv.clock, seed="i")
    done: list[int] = []
    started = threading.Event()

    def step(n: int) -> object:
        def run() -> None:
            done.append(n)
            if n == 0:
                started.set()
                while not pools.stopping:
                    time.sleep(0.01)

        return run

    pools.register(
        "batch", pool="io", handler=lambda ctx: ctx.run_steps([step(0), step(1), step(2)])
    )
    pools.submit("batch", dedupe_key="b1")
    huey = pools.huey("io")
    worker = threading.Thread(target=lambda: huey.execute(huey.dequeue()), daemon=True)
    worker.start()
    assert started.wait(5)
    report = GracefulShutdown(pools, instance_lock=lock, checkpoint_s=5).run()
    worker.join(5)
    assert (report.finished, report.abandoned) == (1, 0)
    assert done == [0]  # the step in flight finished, the next one did not start
    assert job_rows(pools)["b1"][0] == "paused"  # parked with its cursor (window.preempted)
    assert pools.ledger.get(job_id_of(pools)).cursor_index == 1


def job_id_of(pools: JobPools) -> str:
    with pools._engine.connect() as conn:
        return str(conn.execute(sa.select(platform.job_view.c.id)).scalar_one())


@pytest.mark.req("ENF-OPS-007")
def test_a_busy_database_does_not_leave_the_slots_held() -> None:
    import sqlite3

    from shelfhold.infrastructure.observability.shutdown import _retried

    calls: list[int] = []

    def release() -> int:
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return 1

    assert _retried(release, pause_s=0) == 1 and len(calls) == 3
    with pytest.raises(sqlite3.OperationalError):
        _retried(lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")), 2, 0)
