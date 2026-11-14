"""Shared fixtures: every test works in its own temporary /data and /store."""

import importlib.util
import sys
import zlib
from pathlib import Path
from types import ModuleType

import pytest

from shelfhold.config import Settings, load_settings

pytest_plugins = [
    "tests.support.leaks",  # the temporary-folder guard (not in a pytester copy)
    "tests.support.threads",  # no thread of the application outlives its test
    "tests.support.logs",  # the logging a test configures does not outlive it
]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    # The disk guard reads the real free space of the folder under test: on a small temporary file
    # system (a 3 GiB tmpfs) every start would be refused as "disk low" and the probes would
    # answer "unknown". Tests of the guard set their own thresholds and usage.
    return {
        "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
        "SHELFHOLD_DISK_PAUSE_GIB": "0",
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
    }


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    return load_settings(env).values


@pytest.fixture(autouse=True)
def _no_periodic_root_judgement(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ops.disk_check` judges the roots every minute and may write a mark: in a test that did not
    ask for it, a job that starts with the app would write while the test seeds the database. The
    tests of the look call `shelfhold.ops.roots.judge_roots` themselves."""
    monkeypatch.setattr("shelfhold.runtime.judge_roots", lambda _watcher: None)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="I/N",
        help="run only shard I of N (1-based); tests are assigned by a hash of their node id",
    )


SUITES = (
    "property",
    "golden",
    "faults",
    "bench",
)  # tests/<suite>/ implies the marker of the same name


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


@pytest.fixture(autouse=True)
def _no_dns_in_url_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The URL rule resolves names (ENF-SEC-001); a test never reaches the network for it. A name
    resolves to nothing unless the test says otherwise (`shelfhold.headless.urls._resolver`)."""
    import shelfhold.headless.urls as urls

    monkeypatch.setattr(urls, "_resolver", lambda: lambda _host: [])
