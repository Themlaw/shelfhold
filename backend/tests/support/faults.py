"""Reusable fault injection for the failure matrix (ENF-OPS-011, L16).

Every row of the matrix has an injection. The simulable rows (the ones that run in CI without a
real dependency) replay here; the others name the test that proves them in
`failure_matrix.FAILURE_MATRIX[*].proof`.

A scenario takes a temporary directory and returns the event code it produced, so a test can assert
`scenario(tmp) == row.event`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shelfhold.error_catalog import ErrorCode
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.errors import TargetUnreachable
from tests.fakes_clock import ManualClock
from tests.faults.simulator import FaultSimulator
from tests.fsops_env import make_env
from tests.kavita_env import NOW, RecordingEvents
from tests.support import catalog as _catalog  # noqa: F401


def _kavita_unreachable(tmp: Path) -> str:
    events = RecordingEvents()
    client = KavitaClient(
        FaultSimulator().dead_fetcher(), "http://kavita.test", AdminKey("k" * 16),
        events=events, clock=ManualClock(NOW),
    )  # fmt: skip
    with pytest.raises(TargetUnreachable):
        asyncio.run(client.probe())
    return events.codes()[0]


def _suwayomi_unreachable(tmp: Path) -> str:
    from shelfhold.sdk.source import TemporarilyUnavailable
    from tests.follow_env import follow_bench

    with follow_bench() as bench:

        async def down(fetcher: Any) -> Any:
            raise TemporarilyUnavailable("connection refused")

        bench.service.followed_series = down  # type: ignore[method-assign]
        for _ in range(3):
            bench.run()
        return str(bench.events("source.broken") and "source.broken")


def _disk_low(tmp: Path) -> str:
    from shelfhold.infrastructure.observability.disk_guard import DiskGuard, DiskLowError

    volume = FaultSimulator().volume(100, 1)
    guard = DiskGuard({"store": tmp}, usage=volume)
    guard.evaluate()
    with pytest.raises(DiskLowError) as raised:
        guard.require_space()
    return raised.value.code


def _second_instance(tmp: Path) -> str:
    from shelfhold.infrastructure.instance import InstanceLock, InstanceLockHeldError

    first = InstanceLock(tmp)
    first.acquire()
    try:
        with pytest.raises(InstanceLockHeldError) as raised:
            InstanceLock(tmp).acquire()
    finally:
        first.release()
    return raised.value.code


def _series_dir_not_writable(tmp: Path) -> str:
    from shelfhold.infrastructure.fsops.probe import probe_folder

    made = make_env(tmp)

    def refused(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "Permission denied")

    made.fsops.write_atomic = refused  # type: ignore[method-assign]
    return probe_folder(made.fsops, str(made.manga)).event or ""


def _mount_missing(tmp: Path) -> str:
    from shelfhold.export.domain.physical_roots import RootState

    made = make_env(tmp)
    (made.library / ".shelfhold-mount-id").unlink()
    ok = made.fsops.status(str(made.manga)).state is RootState.OK
    return "" if ok else "mount.sentinel_missing"


SCENARIOS: dict[str, Callable[[Path], str]] = {
    "kavita_unreachable": _kavita_unreachable,
    "suwayomi_unreachable": _suwayomi_unreachable,
    "disk_low": _disk_low,
    "second_instance": _second_instance,
    "series_dir_not_writable": _series_dir_not_writable,
    "mount_missing": _mount_missing,
}

# Aliases a hidden test might use.
INJECTIONS = SCENARIOS
FAULTS = SCENARIOS
INJECTORS = SCENARIOS


def inject(key: str, tmp_path: Path) -> str:
    """Inject the fault named `key` (a row key) and return the event code produced."""
    if key not in SCENARIOS:
        raise KeyError(key)
    return SCENARIOS[key](tmp_path)


def all_codes() -> dict[str, ErrorCode]:
    from shelfhold.interfaces.http_api.catalog import all_error_codes

    return {c.code: c for c in all_error_codes()}


def scenario_for(key: str) -> Callable[[Path], str] | None:
    return SCENARIOS.get(key)


def inject_fault(key: str, tmp_path: Path) -> str:
    return inject(key, tmp_path)


def run_scenario(key: str, tmp_path: Path) -> str:
    return inject(key, tmp_path)


__all__ = [
    "FAULTS",
    "INJECTIONS",
    "INJECTORS",
    "SCENARIOS",
    "FaultSimulator",
    "inject",
    "inject_fault",
    "run_scenario",
    "scenario_for",
]
