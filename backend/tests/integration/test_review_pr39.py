"""Regression tests for the adversarial review of PR #39 (L22): every proof of concept became a test
that now passes only because the weakness is gone."""

import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
import structlog
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.export.application.intents import IntentJournal, RenameStep
from shelfhold.export.application.locks import LockBusyError, LockLostError, ResourceLocks
from shelfhold.export.application.plans import PlanOp, PlanStore, StalePlanError
from shelfhold.infrastructure.fsops import confined as confined_mod
from shelfhold.infrastructure.instance import (
    InstanceLock,
    InstanceLockHeldError,
    Lease,
    LeaseConflictError,
    LeaseLostError,
    WriteSessions,
)
from shelfhold.infrastructure.migrate import MigrationStateFile, migrate, open_intent_count
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.main import create_app
from tests.fakes_clock import ManualClock
from tests.fsops_env import INSTANCE_ID, make_env
from tests.integration.test_instance_lock import HOLDER
from tests.intents_env import NOW, make_intent_env
from tests.unit.test_architecture_contracts import FSOPS_CALLERS, path_write_offences

ME = "01JBZ3X5QH8M2N4P6R7S9T0AAA"
OTHER = "01JBZ3X5QH8M2N4P6R7S9T0BBB"
DEAD = "01JBZ3X5QH8M2N4P6R7S9T0DDD"
JOB_A = "01JBZ3X5QH8M2N4P6R7S9T0V1A"
JOB_B = "01JBZ3X5QH8M2N4P6R7S9T0V1B"
WORK = "01JBZ3X5QH8M2N4P6R7S9T0W01"


def lock_rows(ienv: object) -> int:
    with ienv.engine.connect() as conn:  # type: ignore[attr-defined]
        return int(
            conn.execute(
                sa.select(sa.func.count()).select_from(coherence.resource_lock)
            ).scalar_one()
        )


# -- 4. the rename is durable before `done`


@pytest.mark.req("EF-EXP-041")
def test_rename_is_flushed_before_done_is_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ienv = make_intent_env(tmp_path)
    src = ienv.fs.manga / "S" / "1.cbz"
    ienv.fs.fsops.write_atomic(str(src), b"x" * 100, parents=True)
    events: list[str] = []
    real_fsync, real_rename = os.fsync, confined_mod.renameat2_noreplace

    def fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def rename(*args: int | str) -> None:
        events.append("rename")
        real_rename(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(confined_mod, "renameat2_noreplace", rename)
    IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock).apply_rename(
        RenameStep(str(src), str(src.with_name("2.cbz"))),
        on_done=lambda conn, intent: events.append("done-txn"),
    )
    between = events[events.index("rename") : events.index("done-txn")]
    assert between.count("fsync") >= 2  # both parent folders, after the rename, before the commit


# -- 3. the lease is checked inside Fsops, for every writer


@pytest.mark.req("ENF-OPS-015")
def test_no_write_path_skips_the_lease_once_enforced(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    env, clock = ienv.fs, ienv.clock
    mine = WriteSessions(env.fsops, instance_id=ME, clock=clock)
    other = WriteSessions(env.fsops, instance_id=OTHER, clock=clock)
    mine.enforce()
    src = env.manga / "S" / "1.cbz"
    with mine.open(str(src)) as session:
        session.write_atomic(str(src), b"a", parents=True)
        IntentJournal(ienv.engine, env.fsops, clock=clock).apply_rename(
            RenameStep(str(src), str(src.with_name("moved.cbz")))
        )  # the journal writes under our lease: fine
    assert (env.manga / "S" / "moved.cbz").exists()
    # Without a session, or under another instance's lease, the raw writers are refused.
    with pytest.raises(LeaseLostError):
        env.fsops.write_atomic(str(env.manga / "S" / "free.cbz"), b"z")
    with other.open(str(env.manga / "S" / "x.cbz")):
        with pytest.raises(LeaseLostError):
            env.fsops.write_atomic(str(env.manga / "S" / "intrus.cbz"), b"z")
        with pytest.raises(LeaseLostError):
            IntentJournal(ienv.engine, env.fsops, clock=clock).apply_rename(
                RenameStep(str(env.manga / "S" / "moved.cbz"), str(env.manga / "S" / "again.cbz"))
            )
    assert (
        not (env.manga / "S" / "intrus.cbz").exists()
        and not (env.manga / "S" / "free.cbz").exists()
    )
    assert (env.manga / "S" / "moved.cbz").exists()


@pytest.mark.req("ENF-OPS-015")
def test_lease_takeover_race_has_one_winner(tmp_path: Path) -> None:
    """B read the dead lease and is pre-empted while A takes it: B's swap must fail."""
    ienv = make_intent_env(tmp_path)
    env, clock = ienv.fs, ienv.clock
    WriteSessions(env.fsops, instance_id=DEAD, clock=clock).take(str(env.library))
    clock.advance(minutes=10)
    a = WriteSessions(env.fsops, instance_id=ME, clock=clock)
    a.release = lambda root: None  # type: ignore[method-assign]  # A keeps its lease open

    class SlowB(WriteSessions):
        fired = False

        def expired(self, lease: Lease) -> bool:
            if not SlowB.fired:
                SlowB.fired = True
                a.take(str(env.library))  # A wins while B is between read and write
            return super().expired(lease)

    b = SlowB(env.fsops, instance_id=OTHER, clock=clock)
    with pytest.raises(LeaseConflictError), b.open(str(env.manga / "b.cbz")):
        pytest.fail("two writers on one root")
    lease = a.read_lease(str(env.library))
    assert lease is not None and lease.instance_id == ME  # A's lease survived intact


@pytest.mark.req("ENF-OPS-015")
def test_dead_lease_taken_when_its_lock_path_is_our_own(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    env, clock = ienv.fs, ienv.clock
    data = tmp_path / "data"
    data.mkdir()
    lock_path = data / ".lock"
    WriteSessions(env.fsops, instance_id=DEAD, clock=clock, lock_path=lock_path).take(
        str(env.library)
    )
    clock.advance(days=30)
    with InstanceLock(data):  # ours: the same path inside our own container
        me = WriteSessions(env.fsops, instance_id=ME, clock=clock, lock_path=lock_path)
        me.take(str(env.library))
        lease = me.read_lease(str(env.library))
        assert lease is not None and lease.instance_id == ME


@pytest.mark.req("ENF-OPS-015")
def test_lease_of_a_previous_run_of_the_same_instance_is_taken_at_once(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    env, clock = ienv.fs, ienv.clock
    data = tmp_path / "data"
    data.mkdir()
    before = WriteSessions(
        env.fsops, instance_id=ME, clock=clock, lock_path=data / ".lock", boot_id="run-1"
    )
    before.take(str(env.library))  # killed: never released, still fresh
    after = WriteSessions(
        env.fsops, instance_id=ME, clock=clock, lock_path=data / ".lock", boot_id="run-2"
    )
    after.take(str(env.library))
    lease = after.read_lease(str(env.library))
    assert lease is not None and lease.boot_id == "run-2"
    foreign = WriteSessions(env.fsops, instance_id=OTHER, clock=clock, boot_id="run-9")
    with pytest.raises(LeaseConflictError):
        foreign.take(str(env.library))


@pytest.mark.req("ENF-OPS-015")
def test_future_dated_lease_counts_as_expired_and_is_logged(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    env = ienv.fs
    ahead = ManualClock(NOW + timedelta(days=365))
    WriteSessions(env.fsops, instance_id=DEAD, clock=ahead).take(str(env.library))
    me = WriteSessions(env.fsops, instance_id=ME, clock=ManualClock(NOW + timedelta(days=30)))
    with structlog.testing.capture_logs() as logs:
        me.take(str(env.library))
    assert any(entry["event"] == "lease_dated_in_the_future" for entry in logs)
    lease = me.read_lease(str(env.library))
    assert lease is not None and lease.instance_id == ME


# -- 2. resource locks: renewal, heartbeat, loss


@pytest.mark.req("EF-EXP-042", "ENF-QUAL-006")
def test_resource_lock_of_a_live_job_cannot_be_stolen(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    with locks.hold(JOB_A, work=WORK, series="dest1/S") as held:
        ienv.clock.advance(minutes=6)
        held.renew()
        ienv.clock.advance(minutes=6)  # 12 minutes in: past the first lease, inside the renewed one
        with pytest.raises(LockBusyError), locks.hold(JOB_B, work=WORK, series="dest1/S"):
            pytest.fail("stole the lock of a live job")
        held.ensure()


@pytest.mark.req("EF-EXP-042")
def test_heartbeat_renews_the_lock_by_itself(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    with locks.hold(JOB_A, work=WORK, series="dest1/S", heartbeat=0.05):
        ienv.clock.advance(minutes=9)
        threading.Event().wait(0.3)  # a few beats at the new time
        ienv.clock.advance(minutes=9)
        with pytest.raises(LockBusyError), locks.hold(JOB_B, work=WORK, series="dest1/S"):
            pytest.fail("the heartbeat did not renew")


@pytest.mark.req("EF-EXP-042")
def test_a_job_that_lost_its_lock_stops_before_its_next_write(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    env = ienv.fs
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    sessions = WriteSessions(env.fsops, instance_id=ME, clock=ienv.clock)
    target = env.manga / "S" / "1.cbz"
    with (
        locks.hold(JOB_A, work=WORK, series="dest1/S") as held,
        sessions.open(str(target), guard=held.ensure) as session,
    ):
        session.write_atomic(str(target), b"first", parents=True)
        ienv.clock.advance(minutes=11)  # no renewal: the lease ran out...
        with locks.hold(JOB_B, work=WORK, series="dest1/S"):  # ...and another job took over
            with pytest.raises(LockLostError):
                held.ensure()
            with pytest.raises(LockLostError):
                session.write_atomic(str(target), b"second", replace=True)
    assert target.read_bytes() == b"first"


# -- 1. plans: claimed once, version checked before each op


@pytest.mark.req("EF-EXP-042", "ENF-QUAL-006")
def test_plan_applied_by_two_concurrent_requests_runs_once(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    gate = threading.Barrier(2, timeout=5)
    seen: set[int] = set()
    armed = [False]

    def version() -> int:
        if armed[0] and threading.get_ident() not in seen:
            seen.add(threading.get_ident())
            gate.wait()  # both requests have read the plan as `planned`
        return 7

    store = PlanStore(ienv.engine, clock=ienv.clock, current_version=version)
    plan = store.create(kind="export", files=[], ops=[PlanOp("write", dst="/x")])
    ran: list[str] = []
    errors: list[BaseException] = []

    def apply() -> None:
        try:
            store.apply(plan.plan_id, if_match=plan.base_version, run=lambda op: ran.append(op.op))
        except BaseException as error:
            errors.append(error)

    armed[0] = True
    threads = [threading.Thread(target=apply) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert ran == ["write"]
    assert len(errors) == 1 and isinstance(errors[0], StalePlanError)
    with ienv.engine.connect() as conn:
        status = conn.execute(sa.select(coherence.export_plan.c.status)).scalar_one()
    assert status == "done"


@pytest.mark.req("EF-EXP-042")
def test_plan_stops_when_the_version_moves_between_operations(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    version = [7]
    store = PlanStore(ienv.engine, clock=ienv.clock, current_version=lambda: version[0])
    plan = store.create(kind="rename_plan", files=[], ops=[PlanOp("mkdir"), PlanOp("touch_mtime")])
    ran: list[str] = []

    def run(op: PlanOp) -> None:
        ran.append(op.op)
        version[0] += 1  # a chapter arrives between two operations

    with pytest.raises(StalePlanError) as raised:
        store.apply(plan.plan_id, if_match=7, run=run)
    assert raised.value.status == 412
    assert ran == ["mkdir"]


@pytest.mark.req("EF-EXP-042")
def test_plan_is_applied_under_the_resource_locks(tmp_path: Path) -> None:
    ienv = make_intent_env(tmp_path)
    locks = ResourceLocks(ienv.engine, clock=ienv.clock)
    store = PlanStore(ienv.engine, clock=ienv.clock, current_version=lambda: 1)
    plan = store.create(kind="rename_plan", files=[], ops=[PlanOp("mkdir")])
    held_during: list[int] = []

    def run(op: PlanOp) -> None:
        with ienv.engine.connect() as conn:
            held_during.append(
                conn.execute(
                    sa.select(sa.func.count()).select_from(coherence.resource_lock)
                ).scalar_one()
            )

    store.apply(
        plan.plan_id,
        if_match=1,
        run=run,
        lock=locks.hold(JOB_A, work=WORK, series="dest1/S"),
    )
    assert held_during == [2]
    with ienv.engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(coherence.resource_lock)
            ).scalar_one()
            == 0
        )


# -- architecture scan: no bypass by a receiver's name

CALLER = sorted(FSOPS_CALLERS)[0]


@pytest.mark.req("EF-EXP-060", "ENF-SEC-012")
@pytest.mark.parametrize(
    "code",
    [
        "disk = Path(x)\ndisk.write_text('y')",
        "session = Path('/library/manga/S/1.cbz')\nsession.unlink()",
        "self._fsops.open('w')",
        "_disk = Path(a)\n_disk.rename(b)",
        "class A:\n    def __init__(self, p: Path) -> None:\n        self._fsops = p\n"
        "    def f(self):\n        self._fsops.unlink()",
        "class A:\n    def __init__(self) -> None:\n        self._disk: Path = Path('/x')\n"
        "    def f(self):\n        self._disk.write_text('y')",
    ],
)
def test_architecture_scan_has_no_name_based_bypass(code: str) -> None:
    assert path_write_offences(code, CALLER) != []


@pytest.mark.req("EF-EXP-060", "ENF-SEC-012")
def test_architecture_scan_allows_only_typed_writers_in_listed_modules() -> None:
    code = (
        "class A:\n    def __init__(self, fsops: Fsops) -> None:\n        self._fsops = fsops\n"
        "    def f(self):\n        self._fsops.unlink('/x')\n"
    )
    assert path_write_offences(code, CALLER) == []
    assert path_write_offences(code, "shelfhold/jobs/elsewhere.py") != []  # not a listed module


# -- migrate and the instance lock


@pytest.fixture
def live_instance(settings: Settings) -> subprocess.Popen[str]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", HOLDER, str(settings.data_dir)], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    yield holder
    holder.kill()
    holder.wait()


@pytest.mark.req("ENF-OPS-015", "ENF-OPS-019")
def test_migrate_refuses_under_a_live_instance_and_leaves_no_migration_lock(
    settings: Settings, live_instance: subprocess.Popen[str]
) -> None:
    with pytest.raises(InstanceLockHeldError):
        migrate(settings)
    assert MigrationStateFile(settings.data_dir).read() is None  # `.lock` first, then migrate.lock


@pytest.mark.req("ENF-OPS-019")
def test_open_intent_count_fails_closed_on_a_locked_database(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute(
            "INSERT INTO fs_intent (id, op, intent_schema_version, src_path, dst_path, state,"
            " epoch) VALUES ('01JBZ3X5QH8M2N4P6R7S9T0V1W','rename','1','/a','/b','applying',0)"
        )
        db.commit()
        db.execute("PRAGMA locking_mode=EXCLUSIVE")
        db.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError):
            open_intent_count(settings.database_path)
        db.rollback()
    assert open_intent_count(settings.database_path) == 1


# -- the real start-up


@pytest.mark.req("ENF-OPS-015")
def test_two_applications_started_on_the_same_volumes_second_refuses(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        assert first.get("/healthz").status_code == 200
        with pytest.raises(InstanceLockHeldError) as raised, TestClient(create_app(settings)):
            pytest.fail("a second instance started")
        assert raised.value.code == "instance.lease_conflict"
    with TestClient(create_app(settings)):  # the first one let go of the lock on shutdown
        pass


@pytest.mark.req("EF-EXP-041", "ENF-OPS-015")
def test_start_up_settles_open_intents_with_the_lease_before_the_jobs(
    settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.infrastructure.db import create_db_engine
    from tests.intents_env import sha

    env = make_env(tmp_path / "volumes")
    migrate(settings)
    src = env.manga / "S" / "1.cbz"
    env.fsops.write_atomic(str(src), b"chapter", parents=True)
    dst = src.with_name("1 renamed.cbz")
    engine = create_db_engine(settings.database_path)
    with engine.begin() as conn:
        conn.execute(
            sa.insert(platform.instance).values(
                instance_id=INSTANCE_ID, jurisdiction="FR", restore_epoch=0
            )
        )
        root = env.expected[0]
        conn.execute(
            sa.insert(platform.physical_root).values(
                root_id=root.root_id, path_entry=str(env.library), mode="rw", generation=3
            )
        )
        conn.execute(
            sa.insert(coherence.fs_intent).values(
                id="01JBZ3X5QH8M2N4P6R7S9T0V1W", op="rename", intent_schema_version="1",
                src_path=str(src), dst_path=str(dst), expected_sha256=sha(b"chapter"),
                state="applying", epoch=0,
            )
        )  # fmt: skip
    engine.dispose()
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
    assert dst.read_bytes() == b"chapter" and not src.exists()
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute("SELECT state FROM fs_intent").fetchone() == ("done",)
    assert not (env.library / ".shelfhold" / "lease").exists()  # released with the session


# -- the instance lock itself


@pytest.mark.req("ENF-OPS-015")
def test_a_planted_symlink_on_the_lock_is_refused_with_the_stable_code(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / ".lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InstanceLockHeldError) as raised:
        InstanceLock(data).acquire()
    assert raised.value.code == "instance.lease_conflict"
    assert not (tmp_path / "elsewhere").exists()


FORKER = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    from shelfhold.infrastructure.instance import InstanceLock

    InstanceLock(Path(sys.argv[1])).acquire()
    if os.fork() == 0:
        time.sleep(5)
        os._exit(0)
    os._exit(0)
    """
)


@pytest.mark.req("ENF-OPS-015")
def test_a_forked_child_does_not_keep_the_instance_lock_alive(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    subprocess.run([sys.executable, "-c", FORKER, str(data)], check=True)  # noqa: S603
    lock = InstanceLock(data)
    lock.acquire()  # the orphaned child dropped its inherited copy
    assert lock.is_held()
    lock.release()


@pytest.mark.req("ENF-OPS-015", "ENF-SEC-005")
def test_rotate_under_a_live_instance_says_to_stop_shelfhold(
    settings: Settings, env: dict[str, str]
) -> None:
    from shelfhold.infrastructure.secrets import diagnostics

    migrate(settings)
    diagnostics.boot_check(settings)
    holder = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", HOLDER, str(settings.data_dir)], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    out = subprocess.run(  # noqa: S603
        [str(Path(sys.executable).with_name("shelfhold")), "secrets", "rotate"],
        env={**os.environ, **env}, capture_output=True, text=True, check=False,
    )  # fmt: skip
    holder.kill()
    holder.wait()
    assert out.returncode != 0
    assert "arrêtez shelfhold" in out.stderr.lower()
