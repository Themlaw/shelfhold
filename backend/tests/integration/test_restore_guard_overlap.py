"""EF-INT-026, EF-EXP-029, I39 (ADR-0044): the restore guard reads the database and the sentinels
as one state.

A write session moves the generation in the database first and re-stamps the sentinel after. A
guard that looked at the database and then at the sentinels, with such a session between the two
looks, saw a sentinel one step ahead of its snapshot and called an ordinary write a restore: the
instance froze for good (`restored_behind` is persisted until the adoption). The overlap is made
deterministic here: the "session" runs inside the guard's own sentinel read, no sleep, no thread.
A real restore (a database older than its disk) must stay refused, overlap or not.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.restore import RestoreGuard
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.schema import platform
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-INT-026", "EF-EXP-029")


class Gated:
    """The guard's `Fsops`, with a hook run at the first sentinel read: `before` runs just
    before the read (the guard has taken its first look at the database), `after` just after."""

    def __init__(
        self,
        inner: Any,
        *,
        before: Callable[[], None] | None = None,
        after: Callable[[], None] | None = None,
    ) -> None:
        self._inner = inner
        self._before = before
        self._after = after

    def sentinel_of(self, root_path: str, *, strict: bool = False) -> Any:
        before, self._before = self._before, None
        if before is not None:
            before()
        found = self._inner.sentinel_of(root_path, strict=strict)
        after, self._after = self._after, None
        if after is not None:
            after()
        return found

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def commit_generation(env: RestoreEnv) -> int:
    """The first half of a write session: the database commits `generation + 1` (I39)."""
    moved = GenerationCounter(env.engine).advance(str(env.library))
    assert moved is not None
    return moved


def session(env: RestoreEnv) -> None:
    """A whole write session as it touches the counter: database first, sentinel after."""
    env.fsops.stamp_sentinel(str(env.library), generation=commit_generation(env))


def guard_over(env: RestoreEnv, **hooks: Callable[[], None]) -> RestoreGuard:
    return RestoreGuard(env.engine, Gated(env.fsops, **hooks), clock=env.ex.clock, bus=env.bus)  # type: ignore[arg-type]


def persisted_freeze(env: RestoreEnv) -> tuple[int, int]:
    with env.engine.connect() as connection:
        row = connection.execute(
            sa.select(platform.instance.c.restore_epoch, platform.instance.c.restore_generation)
        ).one()
    return int(row.restore_epoch), int(row.restore_generation)


def test_a_session_between_the_two_looks_is_not_a_restore() -> None:
    with restore_env() as env:
        status = guard_over(env, before=lambda: session(env)).assess()
        assert not status.behind, status.findings
        assert [f.state for f in status.findings] == [RootState.OK]


def test_a_session_between_the_two_looks_never_freezes_the_instance() -> None:
    """The start-up and the write gate call `enter_restored_behind`: a false verdict there is
    written (epoch, freeze, revoked credentials) and lasts until the adoption."""
    with restore_env() as env:
        before = persisted_freeze(env)
        status = guard_over(env, before=lambda: session(env)).enter_restored_behind()
        assert not status.behind
        assert persisted_freeze(env) == before
        assert env.events("instance.restored_behind") == []


@pytest.mark.parametrize("sessions", [1, 2])
def test_a_session_after_the_sentinel_read_is_never_a_restore(sessions: int) -> None:
    """Sentinel read first, then the database: the database can only be ahead of what was read."""
    with restore_env() as env:

        def run() -> None:
            for _ in range(sessions):
                session(env)

        status = guard_over(env, after=run).assess()
        assert not status.behind, status.findings


def test_a_session_stopped_between_commit_and_stamp_is_consistent() -> None:
    with restore_env() as env:
        status = guard_over(env, after=lambda: commit_generation(env)).assess()
        assert not status.behind, status.findings


def restored_older(env: RestoreEnv) -> None:
    """A database put back from a backup taken before two more write sessions: the disk is
    ahead of it."""
    backup = env.backup()
    env.add_chapters(3, 3)
    env.add_chapters(4, 4)
    env.restore(backup)


def test_a_real_restore_is_still_refused() -> None:
    with restore_env() as env:
        restored_older(env)
        status = env.guard().assess()
        assert status.behind
        assert [f.state for f in status.behind_roots] == [RootState.RESTORED_BEHIND]


def test_a_real_restore_is_still_refused_while_a_session_overlaps() -> None:
    """The overlap moves both counters by one: the disk is still ahead of the database."""
    with restore_env() as env:
        restored_older(env)
        status = guard_over(env, before=lambda: session(env)).assess()
        assert status.behind, status.findings
        assert [f.state for f in status.behind_roots] == [RootState.RESTORED_BEHIND]


def test_a_real_restore_is_still_refused_when_a_session_follows_the_read() -> None:
    with restore_env() as env:
        restored_older(env)
        status = guard_over(env, after=lambda: commit_generation(env)).assess()
        assert status.behind, status.findings
