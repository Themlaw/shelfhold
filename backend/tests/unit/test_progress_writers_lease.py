"""M5 L22a (EF-LEC-014): every writer of a position or of a "read" mark goes through the lease.

A job that rewrites the files of a Work holds `work:<id>` and looks at the guard "chapter read"
under it. A writer of positions that does not take the same lock reopens the window between the
guard and the swap. The list of the writers below is closed: a new module that inserts a position,
or journals a read mark, fails here until it is listed with the way it is leased.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.req("EF-LEC-014")

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"

# What writes a row of `reading_position` or journals `read_units`: the call that does it.
WRITES = re.compile(
    r"insert_position\(|sa\.insert\(reading\.reading_position\)|sa\.insert\(position\)"
    r"|\.values\(read_units=|mark_traversed_in_frame\(|_mark_traversed\("
)

# module -> (how it is leased, the text that proves it). `lease`: the module takes the Work's
# lease itself; `job`: it only runs under the lock of the job that holds the Work; `callee`: a
# library function, never imported by a surface, whose callers are listed here by their own proof.
LEASED: dict[str, tuple[str, str]] = {
    "reading/application/positions.py": ("lease", "progress_leased("),
    "reading/application/kosync.py": ("lease", "_hold_work"),
    "reading/application/sync.py": ("lease", "progress_leased("),
    "export/application/assembly.py": ("job", "self._locks.hold("),
    "reading/application/progress.py": ("callee", "def insert_position"),
    "reading/application/server_state.py": ("callee", "insert_position"),
    "reading/application/transfer.py": ("callee", "insert_position"),
}

# the callers of the `callee` modules, each with the lease it takes
CALLERS: dict[str, str] = {
    "ops/sheet.py": "build_progress_lease(",  # prefer_edition -> transfer.carry_over
    "reading/application/sync.py": "progress_leased(",  # record_server_state
}


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_every_writer_of_positions_is_listed() -> None:
    found = {
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "migrations" not in p.parts and WRITES.search(p.read_text(encoding="utf-8"))
    }
    assert found == set(LEASED), (
        f"a writer of positions or marks is not listed (or no longer writes): "
        f"{sorted(found ^ set(LEASED))}"
    )


@pytest.mark.parametrize("module", sorted(LEASED))
def test_each_writer_takes_the_lease_or_runs_under_the_jobs_lock(module: str) -> None:
    _how, proof = LEASED[module]
    assert proof in _read(module), f"{module} no longer shows how it is leased ({proof!r})"


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_the_callers_of_the_library_writers_take_the_lease(caller: str) -> None:
    assert CALLERS[caller] in _read(caller)


def test_no_surface_calls_a_library_writer_directly() -> None:
    """An HTTP route or a CLI command reaches a position only through a service that is listed."""
    direct = re.compile(r"insert_position\(|record_server_state\(|carry_over\(|prefer_edition\(")
    allowed = {"ops/sheet.py"}  # leased: `CALLERS`
    offenders = [
        p.relative_to(SRC).as_posix()
        for sub in ("interfaces", "ops", "headless")
        for p in (SRC / sub).rglob("*.py")
        if direct.search(p.read_text(encoding="utf-8"))
        and p.relative_to(SRC).as_posix() not in allowed
    ]
    assert offenders == []
