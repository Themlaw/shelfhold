"""ENF-OPS-006: the disk guard's thresholds and its hysteresis (no I/O: the usage is injected)."""

from pathlib import Path

import pytest

from shelfhold.infrastructure.observability.disk_guard import (
    DiskGuard,
    DiskLowError,
    DiskThresholds,
    Usage,
)

GIB = 1024**3


class FakeUsage:
    def __init__(self, total_gib: float, free_gib: float) -> None:
        self.total = int(total_gib * GIB)
        self.free = int(free_gib * GIB)

    def __call__(self, _path: Path) -> Usage:
        return Usage(total=self.total, free=self.free)


def guard_over(usage: FakeUsage) -> DiskGuard:
    return DiskGuard({"store": Path("/store")}, usage=usage)


@pytest.mark.req("ENF-OPS-006")
def test_disk_guard_hysteresis_on_32gib_fs() -> None:
    usage = FakeUsage(32, 20)
    guard = guard_over(usage)
    assert not guard.evaluate().paused
    usage.free = int(9.9 * GIB)  # under 10 GiB: pause (5 % of 32 GiB is only 1.6 GiB)
    state = guard.evaluate()
    assert state.paused and state.low == ("store",)
    assert [c.low for c in state.changes] == [True]
    usage.free = 12 * GIB  # above the pause threshold, under max(8 %, 15 GiB): still paused
    assert guard.evaluate().paused
    assert guard.evaluate().changes == ()  # no flapping, nothing new to announce
    usage.free = 15 * GIB  # resume at max(8 %, 15 GiB)
    state = guard.evaluate()
    assert not state.paused and [c.low for c in state.changes] == [False]
    usage.free = 12 * GIB  # back under the resume mark but over the pause mark: stays running
    assert not guard.evaluate().paused


@pytest.mark.req("ENF-OPS-006")
def test_thresholds_are_the_spec_defaults_and_adjustable() -> None:
    limits = DiskThresholds()
    big = 1000 * GIB
    assert limits.pause_below(big) == 50 * GIB  # 5 % beats 10 GiB on a big volume
    assert limits.resume_at(big) == 80 * GIB  # 8 % beats 15 GiB
    assert limits.pause_below(32 * GIB) == 10 * GIB
    assert limits.resume_at(32 * GIB) == 15 * GIB
    custom = DiskThresholds(pause_percent=1, pause_gib=1, resume_percent=2, resume_gib=2)
    assert custom.pause_below(32 * GIB) == 1 * GIB


@pytest.mark.req("ENF-OPS-006")
def test_the_guard_refuses_writes_while_low_and_names_the_code() -> None:
    usage = FakeUsage(100, 50)
    guard = guard_over(usage)
    guard.evaluate()
    guard.require_space()  # plenty of room: no error
    usage.free = 1 * GIB
    guard.evaluate()
    with pytest.raises(DiskLowError) as raised:
        guard.require_space()
    assert raised.value.code == "disk.low"
    assert guard.triggered()


@pytest.mark.req("ENF-OPS-006")
def test_vacuum_into_needs_twice_the_database_size_free() -> None:
    usage = FakeUsage(100, 50)
    guard = guard_over(usage)
    assert guard.has_room_for_vacuum(database_bytes=20 * GIB, volume="store")
    assert not guard.has_room_for_vacuum(database_bytes=30 * GIB, volume="store")
