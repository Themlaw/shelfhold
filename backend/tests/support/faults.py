"""Reusable fault injection for the failure matrix (ENF-OPS-011, L16).

Every row of the matrix has an injection. The simulable rows (the ones that run in CI without a
real dependency) replay here; the others name the test that proves them in
`failure_matrix.FAILURE_MATRIX[*].proof`.

A scenario takes a temporary directory and plays the fault against the real production code.
`SCENARIOS` maps a row key to the event code it produced (`scenario(tmp) == row.event`);
`OBSERVATIONS` maps it
to an `Observed`: what Shelfhold did (paused or refused, the waiting state, the content failures
counted), read from the production objects and never from the matrix.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.errors import TargetUnreachable
from tests.fakes_clock import ManualClock
from tests.faults.simulator import FaultSimulator
from tests.fsops_env import make_env
from tests.kavita_env import NOW, RecordingEvents


@dataclass(frozen=True)
class Observed:
    code: str  # the event code the fault produced
    paused: bool  # the queue concerned waits (or the start is refused / the writes blocked)
    waiting_state: str | None  # reported by the production code; None if it self-heals
    counted_failures: int  # content failures counted against a source


def _kavita_unreachable(tmp: Path) -> Observed:
    events = RecordingEvents()
    client = KavitaClient(
        FaultSimulator().dead_fetcher(), "http://kavita.test", AdminKey("k" * 16),
        events=events, clock=ManualClock(NOW),
    )  # fmt: skip
    with pytest.raises(TargetUnreachable):
        asyncio.run(client.probe())
    assert not client.health.paused  # the exports wait, the destination is not disabled
    state = client.health.waiting_state
    return Observed(events.codes()[0], True, state and str(state), 0)


def _suwayomi_unreachable(tmp: Path) -> Observed:
    from shelfhold.sdk.source import TemporarilyUnavailable
    from tests.follow_env import follow_bench

    with follow_bench() as bench:

        async def down(fetcher: Any) -> Any:
            raise TemporarilyUnavailable("connection refused")

        bench.service.followed_series = down  # type: ignore[method-assign]
        report = None
        for _ in range(3):
            report = bench.run()
        code = "source.broken" if bench.events("source.broken") else ""
        assert report is not None
        return Observed(
            code, bool(report.unavailable), None, len(bench.rows("SELECT * FROM source_failure"))
        )


def _disk_low(tmp: Path) -> Observed:
    from shelfhold.infrastructure.observability.disk_guard import DiskGuard, DiskLowError

    guard = DiskGuard({"store": tmp}, usage=FaultSimulator().volume(100, 1))
    state = guard.evaluate()
    with pytest.raises(DiskLowError) as raised:
        guard.require_space()
    return Observed(raised.value.code, state.paused, None, 0)


def _second_instance(tmp: Path) -> Observed:
    from shelfhold.infrastructure.instance import InstanceLock, InstanceLockHeldError

    first = InstanceLock(tmp)
    first.acquire()
    try:
        with pytest.raises(InstanceLockHeldError) as raised:
            InstanceLock(tmp).acquire()
    finally:
        first.release()
    return Observed(raised.value.code, True, None, 0)


def _series_dir_not_writable(tmp: Path) -> Observed:
    from shelfhold.infrastructure.fsops.probe import probe_folder

    made = make_env(tmp)

    def refused(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "Permission denied")

    made.fsops.write_atomic = refused  # type: ignore[method-assign]
    report = probe_folder(made.fsops, str(made.manga))
    state = "permission_denied" if report.errno_name in ("EACCES", "EPERM") else None
    return Observed(report.event or "", not report.ok, state, 0)


def _mount_missing(tmp: Path) -> Observed:
    from shelfhold.export.domain.physical_roots import RootState

    made = make_env(tmp)
    (made.library / ".shelfhold-mount-id").unlink()
    status = made.fsops.status(str(made.manga)).state
    code = "" if status is RootState.OK else "mount.sentinel_missing"
    return Observed(code, status is not RootState.OK, "mount_sentinel_missing" if code else None, 0)


OBSERVATIONS: dict[str, Callable[[Path], Observed]] = {
    "kavita_unreachable": _kavita_unreachable,
    "suwayomi_unreachable": _suwayomi_unreachable,
    "disk_low": _disk_low,
    "second_instance": _second_instance,
    "series_dir_not_writable": _series_dir_not_writable,
    "mount_missing": _mount_missing,
}


def _code_of(play: Callable[[Path], Observed]) -> Callable[[Path], str]:
    return lambda tmp: play(tmp).code


SCENARIOS: dict[str, Callable[[Path], str]] = {
    key: _code_of(play) for key, play in OBSERVATIONS.items()
}

__all__ = ["OBSERVATIONS", "SCENARIOS", "Observed"]
