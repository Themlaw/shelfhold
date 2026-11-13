"""The read behind `/healthz` only reads: it judges no root (that looks at the disks and may write
a mark) and takes no write lock, so a probe, or the refresh that follows a command, never contends
with the job the command has just started (`export.run` failed with an `OperationalError` on its
first attempt, 20 ms after `jobs run`, while the status was judging the roots)."""

from __future__ import annotations

import threading
import time

import pytest
import sqlalchemy as sa

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.schema import platform
from shelfhold.ops import OpsServices
from shelfhold.ops.ports import WaitingItem
from tests.export_env import ExportEnv, export_env
from tests.ops_env import add_job

pytestmark = pytest.mark.req("ENF-OPS-003")


class _Roots:
    """What the operating services ask of the roots: judging them is `refresh`."""

    def __init__(self) -> None:
        self.judged = 0

    def refresh(self) -> None:
        self.judged += 1

    def waiting(self) -> list[WaitingItem]:
        return [WaitingItem("root_lost", "export", "/library", "lost", "shelfhold root reinit", True)]


@pytest.fixture
def env():  # type: ignore[no-untyped-def]
    with export_env() as env:
        yield env


def _ops(env: ExportEnv, roots: _Roots) -> OpsServices:
    return OpsServices(
        env.engine,
        clock=env.clock,
        bus=EventBus(env.clock),
        roots=roots,  # type: ignore[arg-type]
    )


def test_the_health_read_judges_no_root_and_still_lists_the_recorded_marks(env: ExportEnv) -> None:
    roots = _Roots()
    ops = _ops(env, roots)
    assert [q.queue for q in ops.paused_queues()] == ["export"]  # the mark as recorded
    assert roots.judged == 0, "a probe judged the roots (disks looked at, a mark may be written)"
    ops.status(full=False)  # `shelfhold status` still judges them first
    assert roots.judged == 1


def test_the_health_read_is_not_held_by_a_write_in_progress_and_does_not_hold_one(
    env: ExportEnv,
) -> None:
    ops = _ops(env, _Roots())
    add_job(env.engine, kind="export.scan", status="paused", code="queue.paused")
    seen: list[list[str]] = []
    errors: list[BaseException] = []
    inside = threading.Event()

    def writer() -> None:  # a job that writes: it holds the write lock for a while
        try:
            with env.engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as conn:
                conn.execute(sa.update(platform.job_view).values(attempts=1))
                inside.set()
                time.sleep(0.6)
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    thread = threading.Thread(target=writer)
    thread.start()
    assert inside.wait(5)
    started = time.perf_counter()
    seen.append([q.queue for q in ops.paused_queues()])
    waited = time.perf_counter() - started
    thread.join(10)
    assert errors == []
    assert waited < 0.3, f"the health read waited {waited:.2f} s for a write"
    assert "export.scan" in seen[0]

    # and the other way round: the writer that comes while the health read is over is not refused
    with env.engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as conn:
        conn.execute(sa.update(platform.job_view).values(attempts=2))
