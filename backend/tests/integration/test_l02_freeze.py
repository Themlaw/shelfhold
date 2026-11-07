"""M1b L02: one source of truth for the restore freeze (D11, EF-INT-026).

Every point that applies the freeze reads the same persisted state: the write sessions
(`Fsops`), the task start (`Scheduler`), the services (`Repatriator.frozen`, `LoadedRoots`).
Only `reconcile --adopt-after-restore` lifts it."""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.repatriate import Repatriator
from shelfhold.export.application.roots import load_roots, open_fsops
from shelfhold.export.domain.path_map import PathMap
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops import Fsops, RootBlockedError
from shelfhold.infrastructure.instance import WriteSessions
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.schema import platform
from shelfhold.jobs.scheduler import Scheduler
from tests.fakes_clock import ManualClock
from tests.integration.test_restore_epoch import restored
from tests.intents_env import NOW
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-INT-026")


class _Sink:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def dispatch(self, job) -> str:
        self.sent.append(job.kind)
        return "t"


def _loaded(env: RestoreEnv):
    with env.engine.connect() as connection:
        paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
        return load_roots(connection, PathMap.same_path(paths))


def _scheduler(env: RestoreEnv, clock: ManualClock) -> tuple[Scheduler, _Sink]:
    sink = _Sink()
    scheduler = Scheduler(env.engine, clock=clock, seed="s", dispatcher=sink)  # type: ignore[arg-type]
    scheduler.add("follow.cycle", scope_key="", cron="* * * * *")
    return scheduler, sink


def _points(env: RestoreEnv, clock: ManualClock) -> dict[str, bool]:
    """What each application point says, each one built afresh from the database."""
    loaded = _loaded(env)
    fresh = open_fsops(loaded, umask=0o022)
    scheduler, _sink = _scheduler(env, clock)
    repatriator = Repatriator(env.engine, Fsops.open(loaded.path_map, loaded.expected),
                              locate=loaded.locate, clock=clock)  # fmt: skip
    with env.engine.connect() as connection:
        read = FreezeState.read(connection).frozen
    return {
        "state": read,
        "loaded_roots": loaded.restore_pending,
        "write_session": fresh.frozen,
        "task_start": scheduler.held,
        "service": repatriator.frozen(),
    }


@pytest.mark.req("EF-INT-026")
def test_single_freeze_source_for_writes_jobs_and_sessions() -> None:
    with restore_env() as env:
        clock = ManualClock(NOW)
        assert set(_points(env, clock).values()) == {False}
        restored(env)
        env.guard().enter_restored_behind()
        assert set(_points(env, clock).values()) == {True}  # one persisted state, five readers
        # a session opened afterwards is refused on every root, whatever the process says
        other = open_fsops(_loaded(env), umask=0o022)
        with pytest.raises(RootBlockedError):
            other.write_atomic(str(env.library / "manga" / "z.cbz"), b"x", parents=True)
        # the scheduler dispatches nothing at the start of a task, with no `hold()` called
        scheduler, sink = _scheduler(env, clock)
        clock.advance(minutes=5)
        assert scheduler.scan().queued == () and sink.sent == []


@pytest.mark.req("EF-INT-026")
def test_freeze_lifted_only_by_adopt_after_restore() -> None:
    with restore_env() as env:
        clock = ManualClock(NOW)
        restored(env)
        env.guard().enter_restored_behind()
        scheduler, sink = _scheduler(env, clock)
        clock.advance(minutes=5)
        scheduler.release()  # the process lets go: the database still says frozen
        env.fsops.unfreeze()
        assert scheduler.held and scheduler.scan().queued == () and sink.sent == []
        assert open_fsops(_loaded(env), umask=0o022).frozen
        assert set(_points(env, clock).values()) == {True}
        adopter = env.adopter()
        adopter.adopt(confirm=adopter.preview().fingerprint)  # the one exit
        assert set(_points(env, clock).values()) == {False}
        clock.advance(minutes=5)
        scheduler, sink = _scheduler(env, clock)
        assert scheduler.scan().queued != ()


def _persist(env: RestoreEnv, state: FreezeState, generation: int) -> None:
    with env.engine.begin() as connection:
        written = state.persist(connection, generation)
    state.follow(written)


@pytest.mark.req("EF-INT-026")
def test_the_state_is_persisted_and_lifted_in_the_database() -> None:
    with restore_env() as env:
        state = FreezeState()
        _persist(env, state, 3)
        with env.engine.connect() as connection:
            assert FreezeState.read(connection).generation == 3 and state.frozen_in(connection)
        state.lift(env.engine)
        with env.engine.connect() as connection:
            assert not FreezeState.read(connection).frozen and not state.frozen_in(connection)


@pytest.mark.req("EF-INT-026")
def test_review_a_failed_commit_leaves_the_state_frozen_like_the_database() -> None:
    with restore_env() as env:
        state = FreezeState()
        _persist(env, state, 2)

        def locked(_connection) -> None:
            raise sa.exc.OperationalError("COMMIT", {}, Exception("database is locked"))

        sa.event.listen(env.engine, "commit", locked)
        try:
            with pytest.raises(sa.exc.OperationalError):
                state.lift(env.engine)
        finally:
            sa.event.remove(env.engine, "commit", locked)
        with env.engine.connect() as connection:
            assert FreezeState.read(connection).frozen  # the database kept the freeze
        assert state.frozen  # and so did the adopter


def _forked_sessions(env: RestoreEnv) -> tuple[WriteSessions, str]:
    """A writer opened before any freeze, over a root whose sentinel is aligned (the /store)."""
    fork = open_fsops(_loaded(env), umask=0o022)
    sessions = WriteSessions(
        fork, instance_id=str(env.sentinel()["instance_id"]), clock=env.ex.clock,
        generations=GenerationCounter(env.engine),
    )  # fmt: skip
    sessions.enforce()
    return sessions, str(env.ex.lib.store)


@pytest.mark.req("EF-INT-026")
def test_review_a_writer_forked_before_the_freeze_stops_in_a_new_session() -> None:
    with restore_env() as env:
        sessions, store = _forked_sessions(env)
        _persist(env, FreezeState(), 11)  # another writer freezes the database
        with pytest.raises(RootBlockedError), sessions.open(store):
            pass
        assert sessions._fsops.frozen  # the fork follows the database


@pytest.mark.req("EF-INT-026")
def test_review_a_session_opened_before_the_freeze_fails_at_its_next_write() -> None:
    with restore_env() as env:
        sessions, store = _forked_sessions(env)
        with sessions.open(store) as session:
            session.write_atomic(f"{store}/before.bin", b"x", parents=True)
            _persist(env, FreezeState(), 11)
            with pytest.raises(RootBlockedError):
                session.write_atomic(f"{store}/after.bin", b"x", parents=True)
        assert not (env.ex.lib.store / "after.bin").exists()
