"""Second counter-review of the write lock taken first (#247): each test here is red on the branch.

- a file that keeps changing exhausts the three attempts of `retry_when_changed` in a row (no
  pause between them) and its `FileChangedError` leaves the plan half applied: the items before it
  are committed, no report comes back;
- the identity looked at under the lock (inode, size, mtime) misses a file rewritten in place
  whose mtime was put back (`rsync --inplace -t`, `touch -r`): the digest taken before the lock is
  handed on as if it were the file's;
- the architecture test still misses deferred writes: a name bound once to a locked connection
  and once to an unlocked one (the pattern of 30+ production functions), a connection kept on an
  attribute or opened through `ExitStack.enter_context`, `begin` taken as a value, and the
  implicit commit of `with sqlite3.connect(...)`.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.export.application import withdrawal as withdrawal_module
from shelfhold.export.application.withdrawal import (
    WithdrawalItem,
    WithdrawalPlan,
    WithdrawalService,
    WithdrawnFile,
)
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops.identity import FileIdentity
from shelfhold.infrastructure.stable_hash import FileChangedError, HashedFiles

pytestmark = pytest.mark.req("ENF-OPS-003")


def test_a_file_that_keeps_changing_does_not_leave_a_withdrawal_half_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Editions withdrawn; the second one's file is being written by a third party (a sync in
    progress). Before #247 its hash was taken under the lock and the move refused it later (the
    file stays, `pending`). Now three attempts fail within the same second and the error escapes
    `_apply_locked` after the first Edition was committed and moved."""
    engine = create_db_engine(tmp_path / "db.sqlite")

    class Fsops:
        writes = 0

        def observe(self, local: str) -> tuple[str, FileIdentity]:
            if local.endswith("busy.cbz"):
                self.writes += 1  # every look sees a few more bytes
                return "file", FileIdentity(ino=7, size=self.writes, mtime_ns=self.writes)
            return "file", FileIdentity(ino=3, size=1, mtime_ns=1)

        def sha256_of(self, _local: str) -> str:
            return "sha256:" + "0" * 64

    committed: list[str] = []

    def commit_locked(
        _journal: Any, item: WithdrawalItem, _p: Any, _n: Any, paths: list[str], hashed: HashedFiles
    ) -> list[tuple[str, str]]:
        for path in paths:
            hashed.verified(path)  # what the real `_commit_locked` does under the lock
        committed.append(item.edition_id)
        return []

    monkeypatch.setattr(
        withdrawal_module,
        "load_context",
        lambda _c, _d: SimpleNamespace(local=lambda rel: f"/lib/{rel}"),
    )
    monkeypatch.setattr(withdrawal_module, "IntentJournal", lambda *_a, **_k: None)
    service = WithdrawalService(engine, Fsops(), clock=SystemClock())  # type: ignore[arg-type]
    service._commit_locked = commit_locked  # type: ignore[method-assign,assignment]
    service._scan_folders = lambda _item: None  # type: ignore[method-assign]
    plan = WithdrawalPlan(
        items=tuple(
            WithdrawalItem(
                edition_id=edition,
                shelf_id="s",
                reason="r",
                files=(WithdrawnFile(destination_id="d", export_item_id=edition, rel_path=rel),),
            )
            for edition, rel in (("e1", "calm.cbz"), ("e2", "busy.cbz"))
        ),
        version="v",
    )
    error: BaseException | None = None
    try:
        service._apply_locked(plan, SimpleNamespace(), datetime.now(UTC))  # type: ignore[arg-type]
    except FileChangedError as exc:
        error = exc
    finally:
        engine.dispose()
    assert error is None or not committed, (
        f"{committed} committed, then {error!r} escaped: the plan is half applied, with no report"
    )


class _DiskFsops:
    """`observe` and `sha256_of` of the real facade, on a plain folder."""

    def observe(self, local: str) -> tuple[str, FileIdentity] | None:
        try:
            st = os.lstat(local)
        except FileNotFoundError:
            return None
        return "file", FileIdentity(st.st_ino, st.st_size, st.st_mtime_ns)

    def sha256_of(self, local: str) -> str:
        return "sha256:" + hashlib.sha256(Path(local).read_bytes()).hexdigest()


def test_a_file_rewritten_in_place_with_its_mtime_put_back_is_seen_under_the_lock(
    tmp_path: Path,
) -> None:
    """Same inode, same size, same mtime, other bytes: `verified` hands on the old digest. The
    ctime (which `utime` cannot put back, `Fsops.fingerprint` already relies on it) would see it."""
    path = tmp_path / "a.cbz"
    path.write_bytes(b"A" * 4096)
    hashed = HashedFiles(_DiskFsops())
    hashed.hash(str(path))
    before = os.stat(path)
    with path.open("r+b") as stream:  # rsync --inplace: the same inode, rewritten
        stream.write(b"B" * 4096)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # rsync -t, touch -r
    assert os.stat(path).st_ino == before.st_ino
    try:
        checked = hashed.verified(str(path))
    except FileChangedError:
        return
    real = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert checked is not None
    assert checked[1] == real, "the digest taken before the lock is not the file's any more"


_BYPASSES = {
    "a_name_bound_to_a_locked_then_an_unlocked_connection": """
        def rebound(engine):
            with write_transaction(engine) as connection:
                connection.execute(INSERT)
            with engine.connect() as connection:  # deferred
                connection.execute(SELECT)
                connection.execute(INSERT)
                connection.commit()
    """,
    "a_connection_kept_on_an_attribute": """
        class Store:
            def __init__(self, engine):
                self._connection = engine.connect()

            def bump(self):
                self._connection.execute(SELECT)
                self._connection.execute(INSERT)
                self._connection.commit()
    """,
    "a_connection_opened_through_an_exit_stack": """
        import contextlib

        def stacked(engine):
            with contextlib.ExitStack() as stack:
                connection = stack.enter_context(engine.connect())
                connection.execute(SELECT)
                connection.execute(INSERT)
                connection.commit()
    """,
    "begin_taken_as_a_value": """
        def aliased(engine):
            begin = engine.begin
            with begin() as connection:
                connection.execute(SELECT)
                connection.execute(INSERT)
    """,
    "the_implicit_commit_of_a_driver_connection": """
        import sqlite3

        def raw(path):
            with sqlite3.connect(path) as db:  # commits on exit; its BEGIN is deferred
                db.execute("SELECT 1")
                db.execute("INSERT INTO t VALUES (1)")
    """,
}


@pytest.mark.parametrize("name", sorted(_BYPASSES))
def test_the_architecture_test_sees_these_deferred_writes_too(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    architecture = importlib.import_module("tests.unit.test_write_lock_architecture")
    (tmp_path / "bypass.py").write_text(textwrap.dedent(_BYPASSES[name]), "utf-8")
    monkeypatch.setattr(architecture, "SRC", tmp_path)
    monkeypatch.setattr(architecture, "DEFERRED_READS", {})
    bad, _used = architecture._violations()
    assert bad, f"{name}: a deferred write transaction passes the architecture test"
