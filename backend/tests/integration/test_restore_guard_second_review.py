"""Second adverse review of the restore guard (EF-INT-026, I39, ADR-0044).

The fix made `RestoreGuard.assess` read the sentinels before the counters of the database, so a
write session that overlaps the look is never read as a restore. This review asked the other
questions and found the guard solid on three of them: every writer commits the database before
it stamps a sentinel (the first stamp of a new root excepted, both counters 1); no code path
changes a root's identity at its path, so the two looks of `assess` always agree on the keys; a
database put back between the two looks is caught, not missed, by the new order.

The hole is elsewhere: the guard is not part of every write. `Runtime.require_writable` takes
the look before the exports and the plan applies, and the ops socket takes one through
`RestoreGuardExit.waiting`, but the jobs that go straight to the lease do not. What
`Composition._hold` opens is the subset of roots whose **own** sentinel still matches; the root
that is ahead is skipped before `WriteSessions.open` can meet it, so the `on_behind` hook the
runtime wires there never fires. A database put back under a running instance (a rollback of the
image, ENF-OPS-008) is therefore written on before any look said `restored_behind`: the
safeguard cycle puts copies under the matching root, the acquire job files what it fetched, the
follow cycle and the team switch replace files, and `ExportPipeline.heal_mirror` (the window
job's door) writes mirrors without calling `_require_writable` at all.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.instance import WriteSessions
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.schema import platform
from tests.restore_env import restore_env

pytestmark = pytest.mark.req("EF-INT-026", "EF-EXP-029")


def test_a_session_on_the_matching_root_writes_nothing_while_the_guard_says_behind() -> None:
    """A database put back under a running instance, the library one session ahead of it and
    the store still matching its row. The guard, asked now, already says `restored_behind`;
    the session a job opens (`Composition._hold`) mounts the store and skips the library, so
    neither the look nor the freeze happens: the store is written and its counter moves on the
    restored database. The instance is read only as a whole until the exit command
    (ADR-0044), not root by root."""
    with restore_env() as env:
        backup = env.backup()
        env.add_chapters(3, 3)  # one session on the library: only its counter moves
        env.restore(backup)  # the base goes back: the library is ahead, the store matches
        assert env.guard().assess().behind  # the restore is there for the guard to see

        def store_counters() -> tuple[int, int]:
            with env.engine.connect() as connection:
                row = connection.execute(
                    sa.select(platform.physical_root.c.generation).where(
                        platform.physical_root.c.path_entry == str(env.ex.lib.store)
                    )
                ).scalar_one()
            return int(row), int(env.sentinel(env.ex.lib.store)["generation"])

        behind: list[bool] = []
        sessions = WriteSessions(
            env.fsops,
            instance_id=str(env.sentinel()["instance_id"]),
            clock=env.ex.clock,
            generations=GenerationCounter(env.engine),
            on_behind=lambda: behind.append(env.guard().enter_restored_behind().behind),
        )  # what `Composition.sessions_for` wires (composition.py:373-377)
        sessions.enforce()
        before = store_counters()

        # what `Composition._hold` opens (composition.py:396-402): the ahead root is not mounted
        mounted = tuple(
            e.path for e in env.loaded.expected
            if env.fsops.root_state(e.path) is RootState.OK
        )
        assert mounted == (str(env.ex.lib.store),)
        kept = env.ex.lib.store / "kept.bin"
        with sessions.open(*mounted):
            env.fsops.write_atomic(str(kept), b"kept", parents=True)
        assert not kept.exists()  # written on a database the guard already calls older
        assert store_counters() == before  # the counter moved on the restored database
