"""ENF-OPS-006 / ENF-OPS-011: low disk pauses the write queues, without retries and without
deleting anything; reads and the UI go on; the queues resume by themselves."""

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
from shelfhold.infrastructure.health import HealthProbe
from shelfhold.infrastructure.observability.disk_guard import DiskGuard, DiskLowError, Usage
from shelfhold.infrastructure.observability.disk_watch import DiskWatcher
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.jobs.huey_app import JobPools
from tests.fakes_clock import ManualClock
from tests.fsops_env import make_env

GIB = 1024**3
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


class Space:
    def __init__(self) -> None:
        self.free = 50 * GIB

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


def rows(engine: Engine) -> dict[str, tuple[str, int, str | None]]:
    j = platform.job_view
    with engine.connect() as conn:
        return {
            r.dedupe_key: (r.status, r.attempts, r.error_code)
            for r in conn.execute(
                sa.select(j.c.dedupe_key, j.c.status, j.c.attempts, j.c.error_code)
            )
        }


@pytest.mark.req("ENF-OPS-006", "ENF-OPS-011")
def test_disk_low_pauses_queue_without_retries(tmp_path: Path, engine: Engine) -> None:
    (tmp_path / "data").mkdir()
    clock = ManualClock(NOW)
    pools = JobPools(tmp_path / "data", engine=engine, clock=clock, seed="i")
    ran: list[str] = []
    pools.register("download", pool="io", handler=lambda job: ran.append(job.dedupe_key))
    pools.register(
        "read_meta", pool="io", handler=lambda job: ran.append(job.dedupe_key), writes=False
    )
    space = Space()
    delivered: list[str] = []
    bus = EventBus(clock)
    bus.subscribe(lambda event: delivered.append(f"{event.type}:{event.payload['volume']}"))
    watcher = DiskWatcher(
        DiskGuard({"store": tmp_path}, usage=space), pools, engine=engine, bus=bus
    )
    kept = tmp_path / "kept.cbz"
    kept.write_bytes(b"chapter")

    pools.submit("download", dedupe_key="c1")
    pools.submit("read_meta", dedupe_key="r1")
    space.free = 4 * GIB
    watcher.check()
    watcher.check()  # still low: announced once
    run_queue(pools)
    assert rows(engine) == {
        "c1": ("paused", 0, "disk.low"),  # parked, no attempt spent, no retry scheduled
        "r1": ("succeeded", 1, None),  # reads are not write queues
    }
    assert ran == ["r1"] and pools.paused_reason == "disk.low"
    pools.submit("download", dedupe_key="c2")  # submitted while low: parked as well
    run_queue(pools)
    assert rows(engine)["c2"] == ("paused", 0, "disk.low")
    assert pools.huey("io").pending_count() == 0  # nothing waits for a retry
    assert delivered == ["disk.low:store"]
    assert kept.read_bytes() == b"chapter"  # the guard never deletes

    space.free = 20 * GIB  # back over max(8 %, 15 GiB)
    watcher.check()
    run_queue(pools)
    assert rows(engine)["c1"] == ("succeeded", 1, None)
    assert rows(engine)["c2"] == ("succeeded", 1, None)
    assert sorted(ran) == ["c1", "c2", "r1"] and pools.paused_reason is None


@pytest.mark.req("ENF-OPS-006")
def test_writes_are_refused_below_the_threshold_and_deletions_are_not(tmp_path: Path) -> None:
    made = make_env(tmp_path)
    space = Space()
    guard = DiskGuard({"library": made.library}, usage=space)
    fsops = Fsops.open(made.path_map, made.expected, write_gate=guard.require_space)
    fsops.boot()
    target = str(made.manga / "a.cbz")
    fsops.write_atomic(target, b"one")
    space.free = 1 * GIB
    guard.evaluate()
    with pytest.raises(DiskLowError):
        fsops.write_atomic(str(made.manga / "b.cbz"), b"two")
    with pytest.raises(DiskLowError):
        fsops.mkdir(str(made.manga / "Series"))
    assert (made.manga / "a.cbz").read_bytes() == b"one" and not (made.manga / "b.cbz").exists()
    fsops.unlink(target)  # freeing space is never blocked by the guard


@pytest.mark.req("ENF-OPS-011")
def test_healthz_lists_paused_queues_with_their_reason(tmp_path: Path, engine: Engine) -> None:
    (tmp_path / "data").mkdir()
    pools = JobPools(tmp_path / "data", engine=engine, clock=ManualClock(NOW), seed="i")
    pools.register("download", pool="io", handler=lambda job: None)
    pools.submit("download", dedupe_key="c1")
    pools.pause_writes("disk.low")
    probe = HealthProbe(Settings(data_dir=tmp_path, store_dir=Path("/srv/store-unused")))
    assert probe.paused_queues() == ["download"]
    assert probe.paused_reasons() == {"download": "disk.low"}
