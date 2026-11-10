"""ENF-PERF-001, EF-INT-015 (M1b L23): the same bound on a database of 320 MiB. The memory of
writing, checking and restoring an archive is the same few tens of MiB as for a small one."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.backup_memory import MIB, measure

pytestmark = [pytest.mark.nightly, pytest.mark.req("EF-INT-015", "ENF-PERF-001")]


def test_a_320_mib_database_is_backed_up_checked_and_restored_in_bounded_memory(
    tmp_path: Path,
) -> None:
    peaks = measure(tmp_path, 320)
    for phase in ("write", "verify", "restore"):
        assert peaks[phase] - peaks["base"] < 40 * MIB, (phase, peaks)
        assert peaks[phase] < 500 * MIB, (phase, peaks)  # the budget of the whole instance
