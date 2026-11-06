"""EF-INT-003: 1 000 tasks, a worker killed by SIGKILL while it consumes them, a restart: no
`database is locked`, the locks are free and the tasks go on.

The worker is a real process running the production pools (`JobPools`, `boot_jobs`, the
embedded Huey consumers on their SQLite files); the handler writes a row per job into the
application database, which is how the test counts what ran."""

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.jobs.huey_app import POOL_WORKERS

TASKS = 1000
KILL_AFTER = 40  # rows written by the handler when the parent pulls the trigger
WORKER = textwrap.dedent(
    """
    import os, sqlite3, sys, time
    import sqlalchemy as sa
    from shelfhold.config import load_settings
    from shelfhold.infrastructure.clock import SystemClock
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.instance import InstanceLock
    from shelfhold.jobs.huey_app import JobPools, boot_jobs

    mode, total = sys.argv[1], int(sys.argv[2])
    settings = load_settings(os.environ).values
    engine = create_db_engine(settings.database_path)
    lock = InstanceLock(settings.data_dir)
    lock.acquire()  # a killed holder frees it by itself: the kernel closes the descriptor
    pools = JobPools(settings.data_dir, engine=engine, clock=SystemClock(), seed="kill9")

    def handler(job):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO ran (key, times) VALUES (:k, 1) "
                    "ON CONFLICT (key) DO UPDATE SET times = times + 1"
                ),
                {"k": job.dedupe_key},
            )
        time.sleep(0.004)

    pools.register("work", pool="io", handler=handler)
    report = boot_jobs(pools, instance_lock=lock, resolve_intents=lambda: None)
    print("booted requeued=%d" % report.requeued, flush=True)
    if mode == "first":
        for i in range(total):
            pools.submit("work", dedupe_key="w%04d" % i)
    pools.start_workers()
    print("consuming", flush=True)
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            left = conn.execute(
                sa.text("SELECT count(*) FROM job_view WHERE status != 'succeeded'")
            ).scalar_one()
        if mode == "resume" and left == 0:
            break
        time.sleep(0.2)
    pools.stop_workers()
    lock.release()
    print("finished", flush=True)
    """
)


def count(database: Path, sql: str) -> int:
    with sqlite3.connect(database, timeout=30) as connection:
        return int(connection.execute(sql).fetchone()[0])


def spawn(settings: Settings, mode: str, tmp_path: Path) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "SHELFHOLD_DATA_DIR": str(settings.data_dir),
        "SHELFHOLD_STORE_DIR": str(settings.store_dir),
    }
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", WORKER, mode, str(TASKS)],
        env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


@pytest.mark.req("EF-INT-003")
def test_1000_tasks_sigkill_worker_no_database_locked_resumes(
    settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    database = settings.database_path
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ran (key TEXT PRIMARY KEY, times INTEGER NOT NULL)")
    first = spawn(settings, "first", tmp_path)
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and first.poll() is None:
            if count(database, "SELECT count(*) FROM ran") >= KILL_AFTER:
                break
            time.sleep(0.05)
        assert first.poll() is None, first.stderr.read() if first.stderr else ""
        first.send_signal(signal.SIGKILL)  # a worker in the middle of its tasks
        _out, err_first = first.communicate(timeout=30)
    finally:
        if first.poll() is None:
            first.kill()
    assert first.returncode == -signal.SIGKILL
    done_at_kill = count(database, "SELECT count(*) FROM ran")
    assert KILL_AFTER <= done_at_kill < TASKS  # killed in the middle, not after the end
    assert count(database, "SELECT count(*) FROM job_view WHERE status != 'succeeded'") > 0

    finished_before = {
        row[0] for row in sqlite3.connect(database).execute(
            "SELECT dedupe_key FROM job_view WHERE status = 'succeeded'"
        )
    }  # fmt: skip
    second = spawn(settings, "resume", tmp_path)  # the restart
    try:
        out, err_second = second.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        second.kill()
        with sqlite3.connect(database) as connection:
            states = connection.execute(
                "SELECT status, huey_task_id IS NULL, count(*) FROM job_view GROUP BY 1, 2"
            ).fetchall()
        pytest.fail(f"the restarted worker did not finish the tasks: {states}")
    assert second.returncode == 0, err_second
    assert "finished" in out
    for stderr in (err_first, err_second):
        assert "database is locked" not in stderr.lower()
        assert "OperationalError" not in stderr
    assert count(database, "SELECT count(*) FROM job_view WHERE status = 'succeeded'") == TASKS
    assert (
        count(database, "SELECT count(*) FROM job_view WHERE status IN ('queued','running')") == 0
    )
    assert count(database, "SELECT count(*) FROM ran") == TASKS  # none lost
    with sqlite3.connect(database) as connection:
        times = dict(connection.execute("SELECT key, times FROM ran").fetchall())
    # a task that had finished is never run again; only those a worker held when it was killed
    # (at most one per worker thread of the pool) may have run twice
    assert all(times[key] == 1 for key in finished_before)
    twice = [key for key, n in times.items() if n != 1]
    assert len(twice) <= POOL_WORKERS["io"], twice
    assert count(database, "SELECT count(*) FROM resource_lock") == 0  # the locks are free
    assert count(database, "SELECT coalesce(sum(in_use), 0) FROM domain_slot") == 0
