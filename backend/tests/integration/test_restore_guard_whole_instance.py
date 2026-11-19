"""EF-INT-026, EF-EXP-029, I39 (ADR-0044): one root behind makes the whole instance read only, and
a sentinel never goes back.

The second review of the restore guard found the guard missing from the sessions that go straight
to the lease; its proof is `test_restore_guard_second_review.py`. Here: the rest of that closure
(the doors of the export pipeline, no false alarm on a healthy instance) and the three points the
first fix postponed: a monotonic stamp, a refusal to advance a generation under a sentinel that
is ahead, a restore counted once. Every race is played through a gate, with no sleep and no
thread.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.restore import RestoredBehindError, RestoreGuard
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.instance import HEARTBEAT_INTERVAL, WriteSessions
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.schema import platform
from tests.integration.test_restore_guard_overlap import Gated, restored_older
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-INT-026", "EF-EXP-029")


def sessions_of(env: RestoreEnv, behind: list[bool]) -> WriteSessions:
    """What `Composition.sessions_for` wires: the guard is asked when a root is ahead."""
    sessions = WriteSessions(
        env.fsops,
        instance_id=str(env.sentinel()["instance_id"]),
        clock=env.ex.clock,
        generations=GenerationCounter(env.engine),
        on_behind=lambda: behind.append(env.guard().enter_restored_behind().behind),
    )
    sessions.enforce()
    return sessions


def counters(env: RestoreEnv, root: Path) -> tuple[int, int]:
    """(database, sentinel) generation of a root."""
    with env.engine.connect() as connection:
        base = connection.execute(
            sa.select(platform.physical_root.c.generation).where(
                platform.physical_root.c.path_entry == str(root)
            )
        ).scalar_one()
    return int(base), int(env.sentinel(root)["generation"])  # type: ignore[call-overload]


def epoch(env: RestoreEnv) -> int:
    with env.engine.connect() as connection:
        return int(connection.execute(sa.select(platform.instance.c.restore_epoch)).scalar_one())


# -- one root behind: the whole instance is read only ----------------------------------------


def test_a_session_on_the_matching_root_freezes_the_instance_when_the_other_is_ahead() -> None:
    """The look and the freeze happen at the session itself, whatever root it was asked for."""
    with restore_env() as env:
        backup = env.backup()
        env.add_chapters(3, 3)
        env.restore(backup)
        behind: list[bool] = []
        store = env.ex.lib.store
        with pytest.raises(RootBlockedError), sessions_of(env, behind).open(str(store)):
            pytest.fail("a root of the instance is ahead of the database: nothing may be written")
        assert behind == [True]
        assert env.fsops.frozen  # read only as a whole, not root by root
        assert len(env.events("instance.restored_behind")) == 1


def test_a_session_asked_for_the_ahead_root_itself_is_still_refused() -> None:
    with restore_env() as env:
        backup = env.backup()
        env.add_chapters(3, 3)
        env.restore(backup)
        behind: list[bool] = []
        with pytest.raises(RootBlockedError), sessions_of(env, behind).open(str(env.library)):
            pytest.fail("the root that is ahead was written")
        assert behind == [True]


def test_a_session_on_one_root_still_writes_when_every_root_is_coherent() -> None:
    """No false alarm: the look at the other roots costs nothing on a healthy instance."""
    with restore_env() as env:
        behind: list[bool] = []
        store = env.ex.lib.store
        before = counters(env, store)
        with sessions_of(env, behind).open(str(store)) as session:
            session.write_atomic(str(store / "kept.bin"), b"kept", parents=True)
        assert (store / "kept.bin").read_bytes() == b"kept"
        assert behind == []
        assert counters(env, store) == (before[0] + 1, before[1] + 1)
        assert not env.fsops.frozen


def test_heal_mirror_is_refused_while_the_instance_is_restored_behind() -> None:
    """The window job's door writes mirrors: it asks the guard like every other door."""
    with restore_env() as env:
        restored_older(env)
        assert env.guard().enter_restored_behind().behind
        with pytest.raises(RestoredBehindError):
            env.pipeline.heal_mirror(env.ex.destination_id)


# -- the look at the other roots, while a session is held ------------------------------------


def spy_on_sentinels(env: RestoreEnv, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = env.fsops.sentinel_of

    def spy(path: str, **options: bool) -> object:
        seen.append(path)
        return real(path, **options)

    monkeypatch.setattr(env.fsops, "sentinel_of", spy)
    return seen


def test_the_sentinels_are_read_once_per_heartbeat_and_only_while_a_session_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENF-OPS-014: the verdict of the look holds for one heartbeat; with no session open, nothing
    is read (a sleeping disk stays asleep)."""
    with restore_env() as env:
        store = env.ex.lib.store
        roots = len(env.loaded.expected)
        seen = spy_on_sentinels(env, monkeypatch)
        with sessions_of(env, []).open(str(store)) as session:
            seen.clear()  # the look of `open` is not the subject
            for number in range(5):
                session.write_atomic(str(store / f"k{number}.bin"), b"k", parents=True)
            assert seen == []  # inside the heartbeat: the verdict of `open` holds
            env.ex.clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
            for number in range(5, 10):
                session.write_atomic(str(store / f"k{number}.bin"), b"k", parents=True)
            assert len(seen) == roots  # one look, every root once, then the verdict holds again
        seen.clear()
        env.ex.clock.advance(seconds=600)
        assert seen == []  # no session, no look


def test_a_session_that_is_held_closes_read_only_when_a_root_comes_back_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with restore_env() as env:
        store = env.ex.lib.store
        behind: list[bool] = []
        kept = store / "kept.bin"
        with pytest.raises(RootBlockedError), sessions_of(env, behind).open(str(store)) as session:
            session.write_atomic(str(store / "first.bin"), b"first", parents=True)
            document = json.loads((env.library / ".shelfhold-mount-id").read_text("utf-8"))
            document["generation"] += 5  # the library comes back ahead of the database
            (env.library / ".shelfhold-mount-id").write_text(json.dumps(document), "utf-8")
            env.ex.clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
            session.write_atomic(str(kept), b"kept", parents=True)
        assert behind == [True]
        assert env.fsops.frozen
        assert not kept.exists()


# -- the stamp of a sentinel never goes back -------------------------------------------------


def test_three_sessions_that_stamp_out_of_order_leave_the_sentinel_at_the_highest() -> None:
    with restore_env() as env:
        library = str(env.library)
        counter = GenerationCounter(env.engine)
        committed = [counter.advance(library) for _ in range(3)]  # three sessions, in order
        assert all(g is not None for g in committed)
        first, second, third = (int(g) for g in committed if g is not None)
        for generation in (third, first, second):  # ...which re-stamp in the other order
            env.fsops.stamp_sentinel(library, generation=generation)
        assert int(env.sentinel()["generation"]) == third  # type: ignore[call-overload]
        status = env.guard().assess()
        assert not status.behind, status.findings
        assert {f.state for f in status.findings} == {RootState.OK}
        assert env.fsops.root_state(library) is RootState.OK
        # and the next write on that root is not refused for a "stale" sentinel
        with sessions_of(env, []).open(library):
            pass


def test_a_lower_stamp_after_a_higher_one_changes_nothing() -> None:
    with restore_env() as env:
        library = str(env.library)
        top = counters(env, env.library)[0] + 4
        env.fsops.stamp_sentinel(library, generation=top)
        env.fsops.stamp_sentinel(library, generation=top - 3)
        assert int(env.sentinel()["generation"]) == top  # type: ignore[call-overload]


@pytest.mark.parametrize("other_fsops", [False, True], ids=["same_fsops", "another_fsops"])
def test_a_tombstone_rewrite_of_the_sentinel_never_takes_its_generation_back(
    monkeypatch: pytest.MonkeyPatch, other_fsops: bool
) -> None:
    """The journal rewrites the whole sentinel from what it read: a session that stamps a higher
    generation in between must keep it (the stamp and the rewrite exclude each other)."""
    from shelfhold.infrastructure.fsops import tombstone_journal

    with restore_env() as env:
        library = str(env.library)
        top = counters(env, env.library)[0] + 2  # the session itself moves the root to + 1
        real = tombstone_journal.write_sentinel
        done: list[bool] = []
        failures: list[BaseException] = []

        # the stamp lock is the process's, not the `Fsops`'s: a second one over the same roots
        stamper = Fsops.open(env.loaded.path_map, env.loaded.expected) if other_fsops else env.fsops

        def stamp() -> None:
            try:
                stamper.stamp_sentinel(library, generation=top)
            except BaseException as error:  # reported by the assertion below
                failures.append(error)

        def racing(root: object, doc: object) -> None:
            if not done:
                done.append(True)
                other = threading.Thread(target=stamp)
                other.start()
                other.join(timeout=0.3)  # under the lock it waits for this rewrite to end
                racing.other = other  # type: ignore[attr-defined]
            real(root, doc)  # type: ignore[arg-type]

        monkeypatch.setattr(tombstone_journal, "write_sentinel", racing)
        with sessions_of(env, []).open(library):
            env.fsops.record_tombstone_kid(library, "kid-1", {"purpose": "test"})
        racing.other.join(timeout=5)  # type: ignore[attr-defined]
        assert done, "the rewrite never ran"
        assert not failures, failures
        assert int(env.sentinel()["generation"]) >= top  # type: ignore[call-overload]


# -- no generation moves under a sentinel that is ahead --------------------------------------


def test_advancing_a_generation_is_refused_when_the_sentinel_is_ahead_of_the_database() -> None:
    with restore_env() as env:
        backup = env.backup()
        env.add_chapters(3, 3)
        env.restore(backup)
        before = counters(env, env.library)
        assert before[1] > before[0]
        with pytest.raises(RootBlockedError) as refused:
            sessions_of(env, [])._advance_generations([str(env.library)])
        assert refused.value.state is RootState.RESTORED_BEHIND
        assert counters(env, env.library) == before  # the database did not move, nor the disk


# -- a restore is counted once ---------------------------------------------------------------


def test_two_concurrent_entries_count_the_restore_once() -> None:
    """The second entry runs between the first one's look and its write: both saw an instance not
    yet counted. One restore moves the epoch by one, publishes one event."""
    with restore_env() as env:
        restored_older(env)
        before = epoch(env)
        other: list[object] = []

        def second() -> None:
            other.append(env.guard().enter_restored_behind())

        first = RestoreGuard(
            env.engine,
            Gated(env.fsops, after=second),
            clock=env.ex.clock,
            bus=env.bus,  # type: ignore[arg-type]
        ).enter_restored_behind()
        assert other and first.behind
        assert epoch(env) == before + 1
        assert len(env.events("instance.restored_behind")) == 1
        assert env.fsops.frozen
