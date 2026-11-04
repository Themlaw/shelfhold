"""E2E fixtures: the compose stack of e2e/compose.yaml on the image under test.

SHELFHOLD_IMAGE names the image (the workflow loads the image of the run, locally:
`SHELFHOLD_IMAGE=shelfhold:local uv run --project ../backend pytest tests`).
"""

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

COMPOSE_FILE = Path(__file__).resolve().parents[1] / "compose.yaml"


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=check,
        timeout=300,
    )


def exec_python(service: str, code: str) -> str:
    return compose("exec", "-T", service, "python", "-c", code).stdout


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
