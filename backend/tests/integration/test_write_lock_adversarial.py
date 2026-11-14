"""Counter-review of the write lock taken first (#247): each test here is red on the branch.

- a read-only path (the root marks read by `/healthz` and `/api/v1/status`) now waits behind any
  writer, up to the busy timeout;
- a whole file is hashed while the write lock is held, before the first write;
- the architecture test misses deferred transactions that write.
"""

from __future__ import annotations

import importlib
import sqlite3
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.export.application.withdrawal import WithdrawalService, WithdrawnFile
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from tests.restore_env import RestoreEnv, restore_env
from tests.root_exits_env import exits

pytestmark = pytest.mark.req("ENF-OPS-003")


@pytest.fixture
def env() -> Iterator[RestoreEnv]:
    with restore_env() as e:
        yield e


def _hold_write_lock(database: str) -> sqlite3.Connection:
    """Another process's writer (a job, the CLI) inside its transaction."""
    other = sqlite3.connect(database, isolation_level=None, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    return other


def test_reading_the_root_marks_does_not_wait_for_a_writer(env: RestoreEnv) -> None:
    """`RootWatcher.refresh` only reads when nothing changed (the common case, every status read
    and every authenticated `/healthz` refresh). Under WAL a read never waits for a writer."""
    _exits, watcher = exits(env)
    watcher.refresh()  # the marks are recorded: the next refresh has nothing to write
    other = _hold_write_lock(str(env.engine.url.database))
    try:
        started = time.monotonic()
        error: BaseException | None = None
        try:
            watcher.refresh()
        except Exception as exc:  # the busy timeout, on the branch
            error = exc
        elapsed = time.monotonic() - started
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert error is None, f"a read-only refresh failed behind a writer: {error!r}"
    assert elapsed < 1.0, f"a read-only refresh waited {elapsed:.1f}s for a writer"


def test_a_whole_file_is_not_hashed_under_the_write_lock(tmp_path: Path) -> None:
    """`_keep_then_move`: the hash is an argument of the first write, so with the lock taken at
    BEGIN it is computed while every other writer waits (a large file on a slow disk: seconds)."""
    database = tmp_path / "db.sqlite"
    engine = create_db_engine(database)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE t (x)")
    seen: dict[str, bool] = {}

    class Fsops:
        def observe(self, _path: str) -> tuple[str, Any]:
            return "file", SimpleNamespace(ino=1, size=1, mtime_ns=1)

        def sha256_of(self, _path: str) -> str:
            probe = sqlite3.connect(database, isolation_level=None, timeout=0)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("ROLLBACK")
                seen["lock_free_while_hashing"] = True
            except sqlite3.OperationalError:
                seen["lock_free_while_hashing"] = False
            finally:
                probe.close()
            return "0" * 64

    class Journal:
        def record(self, *, within: Any, **_: Any) -> str:
            within.exec_driver_sql("INSERT INTO t VALUES (1)")
            return "intent"

    service = WithdrawalService(engine, Fsops(), clock=SystemClock())  # type: ignore[arg-type]
    service._keep_copy = lambda *_a: True  # type: ignore[method-assign]
    service._move = lambda *_a: True  # type: ignore[method-assign]
    file = WithdrawnFile(destination_id="d", export_item_id="i", rel_path="a.cbz")
    try:
        assert service._keep_then_move(Journal(), file, "/lib/a.cbz")  # type: ignore[arg-type]
    finally:
        engine.dispose()
    assert seen == {"lock_free_while_hashing": True}


_BYPASSES = {
    "a_second_connection_in_a_locked_function": """
        def two(engine):
            immediate = engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with immediate.begin() as c:
                c.execute(INSERT)
            conn = engine.connect()
            with conn.begin():  # deferred: reads, then writes
                conn.execute(SELECT)
                conn.execute(INSERT)
    """,
    "the_words_in_a_comment": """
        def comment(db):
            # not shelfhold_begin="BEGIN IMMEDIATE" here
            with db.begin() as c:
                c.execute(SELECT)
                c.execute(INSERT)
    """,
    "a_driver_connection": """
        import sqlite3

        def raw(path):
            c = sqlite3.connect(path)
            c.execute("SELECT 1")
            c.execute("INSERT INTO t VALUES (1)")
            c.commit()
    """,
    "a_commit_in_a_helper": """
        def outer(engine):
            with engine.connect() as c:
                _write(c)

        def _write(c):
            c.execute(SELECT)
            c.execute(INSERT)
            c.commit()
    """,
    "module_level_code": """
        with ENGINE.begin() as c:
            c.execute(INSERT)
    """,
}


@pytest.mark.parametrize("name", sorted(_BYPASSES))
def test_the_architecture_test_sees_every_deferred_write(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    architecture = importlib.import_module("tests.unit.test_write_lock_architecture")
    (tmp_path / "bypass.py").write_text(textwrap.dedent(_BYPASSES[name]), "utf-8")
    monkeypatch.setattr(architecture, "SRC", tmp_path)
    monkeypatch.setattr(architecture, "DEFERRED_READS", {})
    bad, _used = architecture._violations()
    assert bad, f"{name}: a deferred write transaction passes the architecture test"
