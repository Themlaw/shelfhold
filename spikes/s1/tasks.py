"""Spike S1 tasks. One SqliteHuey instance per pool, like the production layout (io, cpu, maint)."""

import os
import sqlite3
import time
from pathlib import Path

from huey import SqliteHuey

import layer

WORKDIR = Path(os.environ.get("S1_DIR", "."))
huey = SqliteHuey("io", filename=str(WORKDIR / "huey-io.db"))
huey_cpu = SqliteHuey("cpu", filename=str(WORKDIR / "huey-cpu.db"))
huey_maint = SqliteHuey("maint", filename=str(WORKDIR / "huey-maint.db"))


def app_db() -> sqlite3.Connection:
    db = sqlite3.connect(WORKDIR / "app.db", timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    return db


@huey.on_startup()
def at_startup() -> None:
    """Single instance (ENF-OPS-007): locks of a dead worker are flushed, stale slots purged."""
    if os.environ.get("S1_FLUSH_LOCKS", "1") == "1":
        huey.flush_locks()
    layer.purge_slots(WORKDIR / "slots.db")


@huey.task()
def record(n: int) -> int:
    """Criterion 1: each task writes to the application database, concurrently with the others."""
    with app_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS done (n INTEGER, at REAL)")
        db.execute("INSERT INTO done VALUES (?, ?)", (n, time.time()))
    return n


@huey.task()
@huey.lock_task("chapter-lock")
def locked_slow(n: int, seconds: float) -> int:
    """Criterion 2: a task holding a named lock when the worker is killed."""
    with app_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS started (n INTEGER, at REAL)")
        db.execute("INSERT INTO started VALUES (?, ?)", (n, time.time()))
    time.sleep(seconds)
    with app_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS done (n INTEGER, at REAL)")
        db.execute("INSERT INTO done VALUES (?, ?)", (n, time.time()))
    return n


@huey.task()
def with_domain_slot(n: int, domain: str, hold: float) -> int:
    """Criterion 4: at most N=2 concurrent tasks per domain (Shelfhold `domain_slot` table)."""
    with layer.domain_slot(WORKDIR / "slots.db", domain, limit=2, task_id=n, hold=hold):
        pass
    return n
