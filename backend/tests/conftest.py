"""Shared fixtures: every test works in its own temporary /data and /store."""

import importlib.util
import sys
import zlib
from pathlib import Path
from types import ModuleType

import pytest

from shelfhold.config import Settings, load_settings


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
    }


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    return load_settings(env).values


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="I/N",
        help="run only shard I of N (1-based); tests are assigned by a hash of their node id",
    )


SUITES = ("property", "golden", "faults")  # tests/<suite>/ implies the marker of the same name


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        for suite in SUITES:
            if f"/tests/{suite}/" in item.path.as_posix():
                item.add_marker(getattr(pytest.mark, suite))
    spec = config.getoption("--shard")
    if not spec:
        return
    index, _, total = str(spec).partition("/")
    if not (index.isdigit() and total.isdigit() and 1 <= int(index) <= int(total)):
        raise pytest.UsageError(f"--shard expects I/N with 1 <= I <= N, got {spec!r}")
    mine = int(index) - 1
    count = int(total)
    kept = [i for i in items if zlib.crc32(i.nodeid.encode()) % count == mine]
    config.hook.pytest_deselected(items=[i for i in items if i not in kept])
    items[:] = kept


@pytest.fixture(scope="session")
def malicious() -> ModuleType:
    """The generator of booby-trapped XML and archives (tests/fixtures/synthetic)."""
    path = Path(__file__).parent / "fixtures" / "synthetic" / "generate_malicious.py"
    spec = importlib.util.spec_from_file_location("synthetic_generate_malicious", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module
