"""Plugin of tests/conftest.py: a fixture that leaves a folder in the system temporary folder
fails the session (a test machine once held 164 `shelfhold-catalog-template-*`)."""

import sys
import tempfile
from pathlib import Path

import pytest

PREFIX = "shelfhold-"


def pytest_configure(config: pytest.Config) -> None:
    """Remember every folder this process makes in the system temporary folder (other sessions
    share it: comparing directory listings would blame them)."""
    made: list[Path] = []
    real = tempfile.mkdtemp

    def mkdtemp(suffix=None, prefix=None, dir=None):  # type: ignore[no-untyped-def]
        path = real(suffix, prefix, dir)
        if (prefix or "").startswith(PREFIX) and dir is None:
            made.append(Path(path))
        return path

    tempfile.mkdtemp = mkdtemp  # type: ignore[assignment]
    config.stash_made = made  # type: ignore[attr-defined]
    config.stash_real = real  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    tempfile.mkdtemp = config.stash_real  # type: ignore[attr-defined]
    leaked = [p for p in config.stash_made if p.exists()]  # type: ignore[attr-defined]
    if leaked:
        sys.stderr.write(f"\nleaked temporary folders: {', '.join(map(str, leaked))}\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(scope="session", autouse=True)
def _catalog_template_root(tmp_path_factory: pytest.TempPathFactory) -> None:
    from tests.catalog_env import use_template_root

    use_template_root(tmp_path_factory.mktemp("catalog-template"))
