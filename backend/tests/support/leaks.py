"""Session hook: a fixture that leaves a folder in the system temporary folder fails the session
(a test machine once held 164 `shelfhold-catalog-template-*`)."""

import sys
import tempfile
from pathlib import Path

import pytest

LEAK_GLOB = "shelfhold-*-template-*"


def leftovers() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob(LEAK_GLOB))


def pytest_configure(config: pytest.Config) -> None:
    config.stash_before = leftovers()  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if hasattr(session.config, "workerinput"):  # an xdist worker: the controller checks
        return
    leaked = sorted(leftovers() - session.config.stash_before)  # type: ignore[attr-defined]
    if leaked:
        sys.stderr.write(f"\nleaked temporary folders: {', '.join(map(str, leaked))}\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
