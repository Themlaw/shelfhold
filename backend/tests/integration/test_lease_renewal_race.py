"""ENF-OPS-015: the heartbeat thread and `WriteSessions.check` both renew the lease of the same
holder. When their renewals meet, the loser of the compare-and-swap must not call the lease lost:
the lease is still ours. A lease that ran out, or that another holder took, is still lost.
Every race here is staged by a hook between the read and the swap: no thread, no real clock."""

from collections.abc import Callable
from pathlib import Path

import pytest

from shelfhold.infrastructure.instance import (
    HEARTBEAT_INTERVAL,
    LEASE_VALIDITY,
    Lease,
    LeaseLostError,
    WriteSessions,
)
from tests.fsops_env import Env
from tests.intents_env import make_intent_env

ME = "01JBZ3X5QH8M2N4P6R7S9T0AAA"
OTHER = "01JBZ3X5QH8M2N4P6R7S9T0BBB"


class Raced(WriteSessions):
    """`expired` runs between the read and the swap of a renewal (`decide`): `during` fires on the
    `skip`-th call from then on (`check` calls `expired` once itself before it renews), once, or at
    every renewal when `always`."""

    during: Callable[[], None] | None = None
    always = False
    skip = 0
    fired = 0
    _seen = 0

    def expired(self, lease: Lease) -> bool:
        if self.during is not None:
            self._seen += 1
            if self._seen > self.skip:
                hook = self.during
                if not self.always:
                    self.during = None
                self.fired += 1
                hook()
        return super().expired(lease)


def setup(tmp_path: Path) -> tuple[Env, Raced, "object"]:
    ienv = make_intent_env(tmp_path)
    env, clock = ienv.fs, ienv.clock
    sessions = Raced(env.fsops, instance_id=ME, clock=clock)
    sessions.take(str(env.library))
    sessions.enforce()
    return env, sessions, clock


@pytest.mark.req("ENF-OPS-015")
def test_a_check_that_meets_the_heartbeat_keeps_the_lease(tmp_path: Path) -> None:
    env, sessions, clock = setup(tmp_path)
    root = str(env.library)
    clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)  # the heartbeat is due
    sessions.skip = 1
    sessions.during = lambda: sessions.renew(root)  # the heartbeat thread renews first
    sessions.check(root)  # was: LeaseLostError "changed under us"
    held = sessions.read_lease(root)
    assert held is not None and sessions._is_mine(held)
    assert not sessions.expired(held)
    env.fsops.write_atomic(str(env.manga / "a.cbz"), b"x", parents=True)  # writes still pass


@pytest.mark.req("ENF-OPS-015")
def test_a_heartbeat_that_meets_a_check_keeps_the_lease(tmp_path: Path) -> None:
    env, sessions, clock = setup(tmp_path)
    root = str(env.library)
    clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
    sessions.during = lambda: sessions.check(root)  # a write checks (and renews) in between
    sessions.renew(root)
    held = sessions.read_lease(root)
    assert held is not None and sessions._is_mine(held)


@pytest.mark.req("ENF-OPS-015")
def test_a_lease_taken_by_another_holder_meanwhile_is_still_lost(tmp_path: Path) -> None:
    env, sessions, clock = setup(tmp_path)
    root = str(env.library)
    clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
    stolen = Lease(OTHER, "boot", clock.now(), "tok").dumps()
    sessions.skip = 1
    sessions.during = lambda: env.fsops.update_lease(root, lambda raw: stolen)
    with pytest.raises(LeaseLostError):
        sessions.check(root)
    held = sessions.read_lease(root)
    assert held is not None and held.instance_id == OTHER  # never written over
    with pytest.raises(LeaseLostError):
        env.fsops.write_atomic(str(env.manga / "b.cbz"), b"x", parents=True)
    assert not (env.manga / "b.cbz").exists()


@pytest.mark.req("ENF-OPS-015")
def test_a_lease_removed_meanwhile_is_still_lost(tmp_path: Path) -> None:
    env, sessions, clock = setup(tmp_path)
    root = str(env.library)
    clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
    sessions.skip = 1
    sessions.during = lambda: env.fsops.update_lease(root, lambda raw: "remove")
    with pytest.raises(LeaseLostError):
        sessions.check(root)
    assert sessions.read_lease(root) is None  # not recreated


@pytest.mark.req("ENF-OPS-015")
def test_a_lease_that_ran_out_meanwhile_is_still_lost(tmp_path: Path) -> None:
    env, sessions, clock = setup(tmp_path)
    root = str(env.library)
    clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)

    def rival_renews_then_time_passes() -> None:
        sessions.renew(root)
        clock.advance(seconds=LEASE_VALIDITY.total_seconds() + 1)

    sessions.skip = 1
    sessions.during = rival_renews_then_time_passes
    with pytest.raises(LeaseLostError):
        sessions.check(root)


@pytest.mark.req("ENF-OPS-015")
def test_an_expired_lease_is_never_renewed(tmp_path: Path) -> None:
    env, sessions, clock = setup(tmp_path)
    root = str(env.library)
    clock.advance(seconds=LEASE_VALIDITY.total_seconds() + 1)
    before = sessions.read_lease(root)
    with pytest.raises(LeaseLostError):
        sessions.renew(root)
    with pytest.raises(LeaseLostError):
        sessions.check(root)
    assert sessions.read_lease(root) == before


@pytest.mark.req("ENF-OPS-015")
def test_endless_contention_ends_as_lost_not_as_a_loop(tmp_path: Path) -> None:
    env, sessions, clock = setup(tmp_path)
    root = str(env.library)
    clock.advance(seconds=HEARTBEAT_INTERVAL.total_seconds() + 1)
    sessions.always = True
    other = Raced(env.fsops, instance_id=ME, clock=clock, boot_id=sessions.boot_id)
    sessions.during = lambda: other.renew(root)  # a fresh renewal slips in at every attempt
    with pytest.raises(LeaseLostError):
        sessions.renew(root)
    assert 1 < sessions.fired <= 5  # bounded
