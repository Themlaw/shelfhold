"""ENF-PERF-001, EF-INT-015 (M1b L23): the backup streams, so its memory does not follow the size
of the database. A database of 64 MiB stands in for the heavy case (the 320 MiB one is
`tests/nightly/test_backup_memory_heavy.py`); a design that held the database, its copy, the tar
and the cipher text would add 4 to 5 times 64 MiB to the base."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.backup_memory import MIB, measure

pytestmark = pytest.mark.req("EF-INT-015", "ENF-PERF-001")


def test_writing_checking_and_restoring_an_archive_stay_in_bounded_memory(tmp_path: Path) -> None:
    peaks = measure(tmp_path, 64)
    for phase in ("write", "verify", "restore"):
        # a few chunks and SQLite's page cache, never the database: under 40 MiB above the floor
        assert peaks[phase] - peaks["base"] < 40 * MIB, (phase, peaks)
        assert peaks[phase] < 500 * MIB, (phase, peaks)  # the budget of the whole instance
