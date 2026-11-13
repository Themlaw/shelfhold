"""Why a test that seeds a running app's database failed with `database is locked` at once, with a
busy timeout of 5 s: a deferred transaction that reads and then writes is not waited for when
another writer has committed in between (its snapshot is outdated), whoever that writer is (a job
that starts with the app). Taking the write lock first (`BEGIN IMMEDIATE`) is waited for."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.db import create_db_engine


def _pair(tmp_path: Path) -> tuple[sa.Engine, sa.Engine]:
    path = tmp_path / "lock.db"
    first = create_db_engine(path)
    with first.begin() as connection:
        connection.execute(sa.text("CREATE TABLE t (x INTEGER)"))
    return first, create_db_engine(path)


def test_a_deferred_read_then_write_is_refused_at_once_when_a_writer_commits_between(
    tmp_path: Path,
) -> None:
    mine, theirs = _pair(tmp_path)
    started = 0.0

    def read_then_write() -> None:
        nonlocal started
        with mine.begin() as connection:
            connection.execute(sa.text("SELECT count(*) FROM t")).scalar_one()
            with theirs.begin() as other:  # a job writes, and commits
                other.execute(sa.text("INSERT INTO t VALUES (1)"))
            started = time.perf_counter()
            connection.execute(sa.text("INSERT INTO t VALUES (2)"))

    with pytest.raises(sa.exc.OperationalError, match="database is locked"):
        read_then_write()
    assert time.perf_counter() - started < 1.0, "the busy timeout was waited for"


def test_the_write_lock_taken_first_is_waited_for(tmp_path: Path) -> None:
    mine, theirs = _pair(tmp_path)
    inside = threading.Event()

    def job() -> None:
        with theirs.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as other:
            other.execute(sa.text("INSERT INTO t VALUES (1)"))
            inside.set()
            time.sleep(0.5)

    thread = threading.Thread(target=job)
    thread.start()
    assert inside.wait(5)
    with mine.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as connection:
        connection.execute(sa.text("SELECT count(*) FROM t")).scalar_one()
        connection.execute(sa.text("INSERT INTO t VALUES (2)"))
    thread.join(10)
    with mine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM t")).scalar_one() == 2
