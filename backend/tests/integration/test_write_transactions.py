"""A transaction that reads and then writes takes the write lock first, on the real paths.

A deferred transaction (`BEGIN`) that reads and then writes is refused at once with `database is
locked` when another writer commits in between (SQLITE_BUSY_SNAPSHOT): the busy timeout does not
apply to an outdated snapshot. Here a second writer commits right before the first write of the
production code under test: with the write lock taken first, that writer is made to wait."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from shelfhold.config import load_settings
from shelfhold.infrastructure import migrate as migrate_module
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import metadata, platform
from shelfhold.interfaces.cli import search as search_cli
from shelfhold.jobs.slots import DomainSlots

pytestmark = pytest.mark.req("ENF-OPS-003")


@contextmanager
def writer_commits_before_the_first_write(
    engine: sa.Engine, other: sa.Engine
) -> Iterator[Callable[[], None]]:
    """After a read of the transaction under test and before its first write, another writer (a
    job, another process) inserts a row and commits. It is given a second to finish: when the
    write lock is already ours it cannot, and it waits for our commit instead."""
    state: dict[str, Any] = {"read": False, "fired": False, "errors": [], "thread": None}

    def interloper() -> None:
        try:
            with other.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as conn:
                conn.execute(
                    sa.insert(platform.domain_slot).values(
                        limit_key="interloper", slots=1, in_use=0
                    )
                )
        except BaseException as error:
            state["errors"].append(error)

    def before(
        _conn: object, _cursor: object, statement: str, *_rest: object
    ) -> None:
        head = statement.lstrip().split(None, 1)[0].upper()
        if head == "SELECT":
            state["read"] = True
        elif head in {"INSERT", "UPDATE", "DELETE"} and state["read"] and not state["fired"]:
            state["fired"] = True
            state["thread"] = threading.Thread(target=interloper)
            state["thread"].start()
            state["thread"].join(1.0)

    def finish() -> None:
        assert state["fired"], "the transaction under test never wrote after reading"
        state["thread"].join(15)
        assert not state["thread"].is_alive()
        assert state["errors"] == []
        with other.connect() as conn:
            keys = conn.execute(sa.select(platform.domain_slot.c.limit_key)).scalars().all()
        assert "interloper" in keys

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield finish
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _pair(tmp_path: Path) -> tuple[sa.Engine, sa.Engine]:
    path = tmp_path / "write.db"
    first = create_db_engine(path)
    metadata.create_all(first)
    return first, create_db_engine(path)


def test_the_boot_release_of_the_job_slots_waits_for_a_writer_instead_of_failing(
    tmp_path: Path,
) -> None:
    mine, theirs = _pair(tmp_path)
    slots = DomainSlots(mine, SystemClock(), lease=timedelta(minutes=1))
    slots.declare("fetch", 2)
    assert slots.try_acquire("fetch")
    with writer_commits_before_the_first_write(mine, theirs) as finish:
        assert slots.release_all() == 1  # reads the leases, then writes: refused when deferred
    finish()
    assert slots.in_use("fetch") == 0


def _begins(engine: sa.Engine) -> list[str]:
    seen: list[str] = []

    def before(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        if statement.lstrip().upper().startswith("BEGIN"):
            seen.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", before)
    return seen


def test_a_migration_takes_the_write_lock_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(
        {"SHELFHOLD_DATA_DIR": str(tmp_path / "data"), "SHELFHOLD_STORE_DIR": str(tmp_path / "s")}
    ).values

    def spied(path: Path) -> sa.Engine:
        engine = create_db_engine(path)
        spy_lists.append(_begins(engine))
        return engine

    spy_lists: list[list[str]] = []
    monkeypatch.setattr(migrate_module, "create_db_engine", spied)
    migrate(settings)
    statements = [s for seen in spy_lists for s in seen]
    assert statements, "no transaction was seen"
    assert set(statements) == {"BEGIN IMMEDIATE"}


def test_a_command_line_rebuild_takes_the_write_lock_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(
        {"SHELFHOLD_DATA_DIR": str(tmp_path / "data"), "SHELFHOLD_STORE_DIR": str(tmp_path / "s")}
    ).values
    migrate(settings)
    seen: list[list[str]] = []

    def spied(path: Path) -> sa.Engine:
        engine = create_db_engine(path)
        seen.append(_begins(engine))
        return engine

    monkeypatch.setattr(search_cli, "create_db_engine", spied)
    args = SimpleNamespace(json=True)
    resolved = SimpleNamespace(values=settings)
    assert search_cli.run_rebuild(args, resolved) == 0  # type: ignore[arg-type]
    statements = [s for each in seen for s in each]
    assert statements
    assert set(statements) == {"BEGIN IMMEDIATE"}
