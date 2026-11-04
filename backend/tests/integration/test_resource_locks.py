"""EF-EXP-042 / ENF-QUAL-006 (I10): resource locks per Work and per target series path."""

import threading
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from shelfhold.export.application.locks import LockBusyError, ResourceLocks

from shelfhold.infrastructure.schema import coherence
from tests.intents_env import make_intent_env

JOB_A = "01JBZ3X5QH8M2N4P6R7S9T0V1A"
JOB_B = "01JBZ3X5QH8M2N4P6R7S9T0V1B"
WORK = "01JBZ3X5QH8M2N4P6R7S9T0W01"
OTHER_WORK = "01JBZ3X5QH8M2N4P6R7S9T0W02"
SERIES = "dest1/Series"


@pytest.mark.req("EF-EXP-042", "ENF-QUAL-006")
def test_concurrent_export_and_rename_same_series_serialized(tmp_path: Path) -> None:
    """I10: a new-chapter export and a rename plan on one series never interleave."""
    ienv = make_intent_env(tmp_path)
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    log: list[str] = []
    export_in = threading.Event()

    def export() -> None:
        with locks.hold(JOB_A, work=WORK, series=SERIES, timeout=5):
            log.append("export:start")
            export_in.set()
            time.sleep(0.3)
            log.append("export:end")

    def rename_plan() -> None:
        export_in.wait(5)
        with locks.hold(JOB_B, work=OTHER_WORK, series=SERIES, timeout=5):  # same path, other Work
            log.append("rename:start")
            log.append("rename:end")

    threads = [threading.Thread(target=export), threading.Thread(target=rename_plan)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert log == ["export:start", "export:end", "rename:start", "rename:end"]
    with ienv.engine.connect() as conn:  # released on exit
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(coherence.resource_lock)
            ).scalar_one()
            == 0
        )


@pytest.mark.req("EF-EXP-042")
def test_work_lock_and_path_lock_are_both_taken_in_a_fixed_order(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    with locks.hold(JOB_A, work=WORK, series=SERIES, timeout=0):
        with ienv.engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    coherence.resource_lock.c.resource, coherence.resource_lock.c.holder_job_id
                )
            ).all()
        assert {r.resource for r in rows} == {f"work:{WORK}", f"path:{SERIES}"}
        assert {r.holder_job_id for r in rows} == {JOB_A}
        # Same Work, other series: still blocked by the work lock, and nothing is left half-taken.
        with (
            pytest.raises(LockBusyError),
            locks.hold(JOB_B, work=WORK, series="dest1/Other", timeout=0),
        ):
            pytest.fail("entered a busy lock")
        with ienv.engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(coherence.resource_lock)
                ).scalar_one()
                == 2
            )


@pytest.mark.req("EF-EXP-042", "ENF-QUAL-006")
def test_two_jobs_never_hold_one_resource_at_once(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    with (
        locks.hold(JOB_A, work=WORK, series=SERIES, timeout=0),
        pytest.raises(LockBusyError),
        locks.hold(JOB_B, work=OTHER_WORK, series=SERIES, timeout=0),
    ):
        pytest.fail("entered a busy lock")


@pytest.mark.req("EF-EXP-042")
def test_lock_of_a_dead_job_is_taken_over_once_its_lease_ran_out(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    dead = locks.hold(JOB_A, work=WORK, series=SERIES, timeout=0)
    dead.__enter__()  # the job is killed: never exits
    with pytest.raises(LockBusyError), locks.hold(JOB_B, work=WORK, series=SERIES, timeout=0):
        pass
    ienv.clock.advance(hours=1)
    with locks.hold(JOB_B, work=WORK, series=SERIES, timeout=0):
        pass


@pytest.mark.req("EF-EXP-042", "ENF-OPS-015")
def test_release_all_at_boot_frees_locks_of_killed_jobs(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    locks.hold(JOB_A, work=WORK, series=SERIES, timeout=0).__enter__()
    assert locks.release_all() == 2
    with locks.hold(JOB_B, work=WORK, series=SERIES, timeout=0):
        pass
