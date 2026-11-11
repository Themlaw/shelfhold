"""Emptying the write-ahead log, where an erasure depends on it (EF-BIB-053).

A purge (and the compaction of the free pages of M1a purges) leaves the pages it blanked in the
`-wal` until a checkpoint folds them into the file and cuts the log. A reader that holds a
snapshot (the running server, while `purge --now` runs as its own process) makes the checkpoint
`busy`: the log, and a title in it, stay. This is the one place that reads that flag, tries again
a few times, and otherwise refuses (`WalBusyError`, catalog `purge.wal_busy`) after leaving a
durable mark that `status` shows ("effacement à finaliser") until a `TRUNCATE` succeeds: the
nightly checkpoint, a start of the server, or `shelfhold purge --checkpoint`.
"""

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

__all__ = [
    "WAL_PENDING_KEY",
    "WalBusyError",
    "settle_pending",
    "truncate_wal",
    "wal_pending_since",
]

log = structlog.get_logger(__name__)

WAL_PENDING_KEY: Final = "purge.wal_pending"  # `app_setting`: since when
TRIES: Final = 3
PAUSE_S: Final = 1.0
BUSY_MS: Final = 1000


class WalBusyError(RuntimeError):
    """A reader still holds the write-ahead log: it keeps the pages an erasure rewrote until it
    leaves. The erasure is done; only the log is not yet empty (`shelfhold purge --checkpoint`)."""


def _connect(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(database, timeout=BUSY_MS / 1000, isolation_level=None)


def truncate_wal(database: Path, *, mark_on_failure: bool = False) -> None:
    """`wal_checkpoint(TRUNCATE)`, its `busy` flag read, `TRIES` times `PAUSE_S` apart. Done: the
    mark of a pending erasure (if any) goes. Still held: `WalBusyError`, after the mark is left
    when `mark_on_failure` (an erasure is waiting on it)."""
    for attempt in range(TRIES):
        connection = _connect(database)
        try:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is not None and not row[0]:
                if _clear_mark(connection):
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the mark's own write
                return
        finally:
            connection.close()
        if attempt + 1 < TRIES:
            time.sleep(PAUSE_S)
    if mark_on_failure:
        _set_mark(database)
    log.warning("wal.busy", database=database.name)
    raise WalBusyError("a reader holds the write-ahead log")


def _clear_mark(connection: sqlite3.Connection) -> bool:
    try:
        cursor = connection.execute("DELETE FROM app_setting WHERE key = ?", (WAL_PENDING_KEY,))
    except sqlite3.Error:
        return False  # no such table (a bare file) or a lock: the next checkpoint clears it
    return cursor.rowcount > 0


def _set_mark(database: Path) -> None:
    connection = _connect(database)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO app_setting(key, value) VALUES (?, ?)",
            (WAL_PENDING_KEY, datetime.now(UTC).isoformat()),
        )
    except sqlite3.Error:  # pragma: no cover (the refusal below is what matters)
        log.warning("wal.mark_failed")
    finally:
        connection.close()


def wal_pending_since(database: Path) -> str | None:
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT value FROM app_setting WHERE key = ?", (WAL_PENDING_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return None if row is None else str(row[0])


def settle_pending(database: Path) -> bool:
    """A pending erasure and a log that can now be emptied: done, the mark gone. True when there
    was nothing pending or it is settled; False when a reader still holds the log."""
    if wal_pending_since(database) is None:
        return True
    try:
        truncate_wal(database)
    except WalBusyError:
        return False
    return True
