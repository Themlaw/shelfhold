"""E2E fixtures: the compose stack of e2e/compose.yaml on the image under test.

SHELFHOLD_IMAGE names the image (the workflow loads the image of the run, locally:
`SHELFHOLD_IMAGE=shelfhold:local uv run --project ../backend pytest tests`).
"""

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from journey import Journey

COMPOSE_FILE = Path(__file__).resolve().parents[1] / "compose.yaml"


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if check and result.returncode:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{result.stderr}")
    return result


def exec_python(service: str, code: str) -> str:
    return compose("exec", "-T", service, "python", "-c", code).stdout


DEFAULT_TEST_SECONDS = 120


@pytest.fixture(autouse=True)
def _short_timeout(request: pytest.FixtureRequest) -> Iterator[None]:
    """Each E2E test has its own short limit (marker `e2e_timeout(seconds)`, setup included): a
    hang fails the test within minutes, with the name of the test, instead of eating the job."""
    marker = request.node.get_closest_marker("e2e_timeout")
    seconds = int(marker.args[0]) if marker else DEFAULT_TEST_SECONDS

    def expired(_signum: int, _frame: object) -> None:
        raise TimeoutError(
            f"{request.node.nodeid} exceeded its {seconds}s limit (e2e_timeout); "
            "the compose logs are printed after this failure"
        )

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(scope="session")
def stack() -> Iterator[None]:
    if not os.environ.get("SHELFHOLD_IMAGE"):
        pytest.skip("SHELFHOLD_IMAGE is not set") if not os.environ.get("CI") else pytest.fail(
            "SHELFHOLD_IMAGE is not set"
        )
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    compose("down", "-v", "-t", "1", check=False)
    try:
        compose("up", "--wait", "-d")
        yield
    finally:
        print(compose("logs", "--no-color", check=False).stdout)
        compose("down", "-v", "-t", "1", check=False)


def wait_for(predicate: Callable[[], bool], seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


@pytest.fixture
def journey() -> Iterator["Journey"]:
    """A fresh journey stack per test: image under test + simulated Kavita and Suwayomi."""
    from journey import Journey

    if not os.environ.get("SHELFHOLD_IMAGE"):
        pytest.skip("SHELFHOLD_IMAGE is not set") if not os.environ.get("CI") else pytest.fail(
            "SHELFHOLD_IMAGE is not set"
        )
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    stack = Journey()
    stack.compose("down", "-v", "-t", "1", check=False)
    try:
        stack.compose("up", "--wait", "-d")
        yield stack
    finally:
        print(stack.compose("logs", "--no-color", check=False).stdout)
        stack.compose("down", "-v", "-t", "1", check=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`E2E_SHARD=<name>` keeps the tests of that shard (e2e/shards.py); a test in no shard is an
    error, so that none is silently left out of the CI."""
    shard = os.environ.get("E2E_SHARD")
    if not shard:
        return
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from shards import SHARDS

    listed = {name for names in SHARDS.values() for name in names}
    orphans = sorted(i.name for i in items if i.name not in listed)
    if orphans:
        raise pytest.UsageError(f"e2e tests in no shard (e2e/shards.py): {orphans}")
    keep = set(SHARDS[shard])
    selected = [i for i in items if i.name in keep]
    config.hook.pytest_deselected(items=[i for i in items if i.name not in keep])
    items[:] = selected
