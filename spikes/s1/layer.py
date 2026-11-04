"""Prototype of the Shelfhold layer on top of Huey (ADR-0005): domain slots and periodic catch-up.

Throwaway code: only what the spike needs to measure the six criteria.
"""

import contextlib
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS domain_slot (domain TEXT, task_id INTEGER, since REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS slot_log (domain TEXT, task_id INTEGER, ev TEXT, at REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS periodic (name TEXT PRIMARY KEY, last_run_at REAL)")
    return db


def purge_slots(path: Path) -> None:
    db = _connect(path)
    db.execute("DELETE FROM domain_slot")
    db.close()


@contextlib.contextmanager
def domain_slot(
    path: Path, domain: str, limit: int, task_id: int, hold: float, poll: float = 0.02
) -> Iterator[None]:
    """Blocking acquire (the spike keeps the worker busy; production re-enqueues with a delay)."""
    db = _connect(path)
    try:
        while True:
            db.execute("BEGIN IMMEDIATE")
            (taken,) = db.execute("SELECT COUNT(*) FROM domain_slot WHERE domain=?", (domain,)).fetchone()
            if taken < limit:
                now = time.time()
                db.execute("INSERT INTO domain_slot VALUES (?,?,?)", (domain, task_id, now))
                db.execute("INSERT INTO slot_log VALUES (?,?, 'acquire', ?)", (domain, task_id, now))
                db.execute("COMMIT")
                break
            db.execute("COMMIT")
            time.sleep(poll)
        try:
            time.sleep(hold)
            yield
        finally:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM domain_slot WHERE domain=? AND task_id=?", (domain, task_id))
            db.execute("INSERT INTO slot_log VALUES (?,?, 'release', ?)", (domain, task_id, time.time()))
            db.execute("COMMIT")
    finally:
        db.close()


def max_concurrency(path: Path) -> dict[str, int]:
    db = _connect(path)
    peaks: dict[str, int] = {}
    current: dict[str, int] = {}
    # 'release' sorts before 'acquire' at equal timestamps, which can only under-estimate nothing
    for domain, ev in db.execute("SELECT domain, ev FROM slot_log ORDER BY at, ev DESC"):
        current[domain] = current.get(domain, 0) + (1 if ev == "acquire" else -1)
        peaks[domain] = max(peaks.get(domain, 0), current[domain])
    db.close()
    return peaks


def catch_up(path: Path, periodics: dict[str, float], enqueue_once, now: float | None = None) -> list[str]:  # type: ignore[no-untyped-def]
    """Run-once policy (B24): one enqueue per periodic whose due date has passed, never N."""
    now = now or time.time()
    db = _connect(path)
    fired: list[str] = []
    for name, interval in periodics.items():
        row = db.execute("SELECT last_run_at FROM periodic WHERE name=?", (name,)).fetchone()
        if row is not None and now - row[0] > interval * 1.5:
            enqueue_once(name)
            fired.append(name)
    db.close()
    return fired


def mark_run(path: Path, name: str) -> None:
    db = _connect(path)
    db.execute(
        "INSERT INTO periodic VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET last_run_at=excluded.last_run_at",
        (name, time.time()),
    )
    db.close()
