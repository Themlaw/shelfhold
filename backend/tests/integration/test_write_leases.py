"""ENF-OPS-015: a lease per physical root, taken for a write session only, checked before each
write. Outside a session nothing is written under /library (the disks may sleep)."""

from datetime import timedelta
from pathlib import Path

import pytest

from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode
from shelfhold.infrastructure.fsops import Fsops, ReadOnlyRootError
from shelfhold.infrastructure.instance import (
    HEARTBEAT_INTERVAL,
    LEASE_VALIDITY,
    LeaseConflictError,
    LeaseLostError,
    WriteSessions,
)
from tests.fakes_clock import ManualClock
from tests.fsops_env import Env, make_env
from tests.intents_env import NOW

ME = "01JBZ3X5QH8M2N4P6R7S9T0AAA"
OTHER = "01JBZ3X5QH8M2N4P6R7S9T0BBB"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


def lease_file(root: Path) -> Path:
    return root / ".shelfhold" / "lease"


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.req("ENF-OPS-015")
def test_spec_constants() -> None:
    assert timedelta(minutes=3) == LEASE_VALIDITY
    assert timedelta(seconds=30) == HEARTBEAT_INTERVAL


@pytest.mark.req("ENF-OPS-015")
def test_lease_taken_per_physical_root(env: Env, clock: ManualClock) -> None:
    sessions = WriteSessions(env.fsops, instance_id=ME, clock=clock)
    with sessions.open(str(env.manga / "S" / "1.cbz")) as session:  # the library root only
        session.write_atomic(str(env.manga / "S" / "1.cbz"), b"x", parents=True)
        held = sessions.read_lease(str(env.library))
        assert held is not None and held.instance_id == ME
        assert lease_file(env.library).exists()
        assert not lease_file(env.books).exists()  # another disk: untouched
        assert sessions.read_lease(str(env.books)) is None
    with sessions.open(str(env.manga / "a.cbz"), str(env.books / "b.cbz")):
        assert lease_file(env.library).exists() and lease_file(env.books).exists()


@pytest.mark.req("ENF-OPS-015")
def test_lease_not_touched_outside_write_session(env: Env, clock: ManualClock) -> None:
    sessions = WriteSessions(env.fsops, instance_id=ME, clock=clock)
    env.fsops.write_atomic(str(env.manga / "S" / "1.cbz"), b"x", parents=True)  # set-up
    before = {r: tree(r) for r in (env.library, env.books)}
    clock.advance(hours=1)
    # Reads, status probes and lease look-ups: no write, no lease, the disk can sleep.
    env.fsops.read_bytes(str(env.manga / "S" / "1.cbz"))
    env.fsops.status(str(env.manga))
    assert sessions.read_lease(str(env.library)) is None
    assert {r: tree(r) for r in (env.library, env.books)} == before
    assert not lease_file(env.library).exists()
    # A session releases its lease at the end, normally or not.
    with sessions.open(str(env.manga / "S" / "1.cbz")):
        assert lease_file(env.library).exists()
    assert not lease_file(env.library).exists()
    with pytest.raises(RuntimeError, match="boom"), sessions.open(str(env.manga / "S")):
        raise RuntimeError("boom")
    assert not lease_file(env.library).exists()
    assert sessions.read_lease(str(env.library)) is None


@pytest.mark.req("ENF-OPS-015")
def test_lease_checked_before_each_write(env: Env, clock: ManualClock) -> None:
    mine = WriteSessions(env.fsops, instance_id=ME, clock=clock)
    theirs = WriteSessions(env.fsops, instance_id=OTHER, clock=clock)
    target = env.manga / "S" / "1.cbz"
    with mine.open(str(target)) as session:
        session.write_atomic(str(target), b"first", parents=True)
        # A fresh lease of another instance blocks the opening of its session...
        with pytest.raises(LeaseConflictError) as conflict:
            theirs.open(str(env.manga / "other.cbz")).__enter__()
        assert conflict.value.code == "instance.lease_conflict"
        # ...and my heartbeat stalled for more than the validity: they legitimately take over.
        clock.advance(seconds=LEASE_VALIDITY.total_seconds() + 1)
        with theirs.open(str(env.manga / "other.cbz")):
            with pytest.raises(LeaseLostError) as lost:
                session.write_atomic(str(target), b"second", parents=True, replace=True)
            assert lost.value.code == "instance.lease_conflict"
            assert target.read_bytes() == b"first"  # the write did not happen
            with pytest.raises(LeaseLostError):
                session.rename(str(target), str(target.with_name("2.cbz")))
            with pytest.raises(LeaseLostError):
                session.unlink(str(target))
            with pytest.raises(LeaseLostError):
                session.mkdir(str(env.manga / "new"))
    assert target.read_bytes() == b"first"
    assert not (env.manga / "new").exists()


@pytest.mark.req("ENF-OPS-015")
def test_heartbeat_renews_the_lease_during_the_batch(env: Env, clock: ManualClock) -> None:
    sessions = WriteSessions(env.fsops, instance_id=ME, clock=clock)
    with sessions.open(str(env.manga / "S" / "1.cbz")) as session:
        opened = sessions.read_lease(str(env.library))
        assert opened is not None
        clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
        session.heartbeat()
        renewed = sessions.read_lease(str(env.library))
        assert renewed is not None and renewed.renewed_at == clock.now() > opened.renewed_at
        clock.advance(seconds=LEASE_VALIDITY.total_seconds() - 5)  # still valid thanks to it
        session.write_atomic(str(env.manga / "S" / "1.cbz"), b"x", parents=True)


@pytest.mark.req("ENF-OPS-015", "EF-EXP-060")
def test_adopt_ro_mode_from_the_database_is_wired_into_fsops(
    tmp_path: Path, clock: ManualClock
) -> None:
    """`physical_root.mode` (L04) decides: a root the database says is `adopt_ro` is never
    written and never leased, whatever the configured path map says."""
    import sqlalchemy as sa

    from shelfhold.export.application.roots import load_roots
    from shelfhold.infrastructure.schema import platform
    from tests.intents_env import prepare_database

    env = make_env(tmp_path)
    engine = prepare_database(tmp_path / "db.sqlite")
    rows = [
        {
            "root_id": env.expected[0].root_id,
            "path_entry": str(env.library),
            "mode": "rw",
            "generation": 3,
        },
        {
            "root_id": "01JBZ3X5QH8M2N4P6R7S9T0CCC",
            "path_entry": str(env.upstream),
            "mode": "adopt_ro",
            "generation": 0,
        },
    ]
    with engine.begin() as conn:
        conn.execute(sa.insert(platform.physical_root), rows)
    configured = PathMap(
        (
            PathEntry(str(env.manga), "/manga"),
            PathEntry(str(env.upstream), "/suwayomi", PathMode.RW),
        )
    )  # the configuration wrongly says rw
    with engine.connect() as conn:
        loaded = load_roots(conn, configured)
    assert [r.root_id for r in loaded.expected] == [
        env.expected[0].root_id
    ]  # no writable root for it
    assert loaded.path_map.entry_for(str(env.upstream / "x")).mode is PathMode.ADOPT_RO
    fsops = Fsops.open(loaded.path_map, loaded.expected)
    sessions = WriteSessions(fsops, instance_id=ME, clock=clock)
    with pytest.raises(ReadOnlyRootError):
        sessions.open(str(env.upstream / "x.cbz")).__enter__()
    assert not lease_file(env.upstream).exists()


@pytest.mark.req("ENF-OPS-015")
def test_expired_lease_is_not_taken_while_the_holder_still_holds_its_flock(
    env: Env, clock: ManualClock, tmp_path: Path
) -> None:
    from shelfhold.infrastructure.instance import InstanceLock

    other_data = tmp_path / "other-data"
    other_data.mkdir()
    holder_lock = InstanceLock(other_data)
    holder_lock.acquire()  # the other instance is alive, even if its heartbeat stalled
    theirs = WriteSessions(env.fsops, instance_id=OTHER, clock=clock, lock_path=holder_lock.path)
    mine = WriteSessions(env.fsops, instance_id=ME, clock=clock)
    path = str(env.manga / "x.cbz")
    theirs.take(str(env.library))
    clock.advance(seconds=LEASE_VALIDITY.total_seconds() + 1)
    with pytest.raises(LeaseConflictError):
        mine.open(path).__enter__()
    holder_lock.release()  # it died: now the expired lease can be taken
    with mine.open(path):
        lease = mine.read_lease(str(env.library))
        assert lease is not None and lease.instance_id == ME


@pytest.mark.req("ENF-OPS-015", "EF-EXP-029")
def test_lease_never_taken_on_a_root_whose_sentinel_is_foreign(
    env: Env, clock: ManualClock
) -> None:
    from shelfhold.infrastructure.fsops import RootBlockedError

    (env.library / ".shelfhold-mount-id").write_text(
        '{"schema_version": "1.0", "root_id": "01JBZ3X5QH8M2N4P6R7S9T0ZZZ", '
        '"instance_id": "01JBZ3X5QH8M2N4P6R7S9T0ZZY", "generation": 3}',
        encoding="utf-8",
    )
    sessions = WriteSessions(env.fsops, instance_id=ME, clock=clock)
    with pytest.raises(RootBlockedError):
        sessions.open(str(env.manga / "x.cbz")).__enter__()
    assert not lease_file(env.library).exists()


@pytest.mark.req("ENF-OPS-015")
def test_a_stale_process_cannot_write_tombstone_lines_or_move_the_sentinel_back(
    env: Env, clock: ManualClock
) -> None:
    """Review of PR #69: two processes of the same instance. The one whose lease ran out must be
    refused for the tombstone writes as for any other (they rewrite the sentinel's kid registry
    from a read that the other process has since moved on)."""
    from shelfhold.infrastructure.fsops import RootBlockedError  # noqa: F401
    from shelfhold.library.tombstones.journal import header_bytes

    other_fsops = Fsops.open(env.path_map, env.expected)
    stale = WriteSessions(env.fsops, instance_id=ME, clock=clock, boot_id="boot-b")
    fresh = WriteSessions(other_fsops, instance_id=ME, clock=clock, boot_id="boot-a")
    stale.enforce()
    fresh.enforce()
    root = str(env.library)
    sentinel = env.library / ".shelfhold" / "root"
    with stale.open(str(env.manga / "x.cbz")):
        clock.advance(seconds=LEASE_VALIDITY.total_seconds() + 1)
        with fresh.open(str(env.manga / "y.cbz")):
            before = sentinel.read_bytes() if sentinel.exists() else None
            with pytest.raises(LeaseLostError):
                env.fsops.append_tombstone_line(root, b"x\n", header=header_bytes())
            with pytest.raises(LeaseLostError):
                env.fsops.record_tombstone_kid(root, "kid-1", {"k": "v"})
            assert (sentinel.read_bytes() if sentinel.exists() else None) == before
