"""ENF-OPS-003 / ENF-OPS-002: the migration lock (flock) drives the `migrating` state."""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import MigrationInProgressError, migrate
from shelfhold.main import create_app
from tests.integration.test_migrations import scripts_with_extra_revision

RUNNER = """
import os, sys
from pathlib import Path
from shelfhold.config import load_settings
from shelfhold.infrastructure.migrate import migrate

migrate(load_settings(os.environ).values, script_location=Path(sys.argv[1]))
"""
SLOW_REVISION = "import time\ntime.sleep(5)"


@pytest.fixture
def slow_migrator(
    settings: Settings, env: dict[str, str], tmp_path: Path
) -> Iterator[subprocess.Popen[bytes]]:
    migrate(settings)
    scripts = scripts_with_extra_revision(tmp_path, SLOW_REVISION)
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(runner), str(scripts)], env={**os.environ, **env}, cwd=tmp_path
    )
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def wait_for_state(client: TestClient, state: str, timeout: float = 20) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body: dict[str, object] = client.get("/healthz").json()
        if body["state"] == state:
            return body
        time.sleep(0.05)
    raise AssertionError(f"/healthz never reported {state}")


@pytest.mark.req("ENF-OPS-003", "ENF-OPS-002")
def test_healthz_reports_migrating_state(
    settings: Settings, slow_migrator: subprocess.Popen[bytes]
) -> None:
    with TestClient(create_app(settings)) as client:
        wait_for_state(client, "migrating")
        # The lock appears slightly before the progress file is written: wait for it.
        deadline = time.monotonic() + 20
        body: dict[str, object] = client.get("/healthz").json()
        while body.get("progress") != {"done": 0, "total": 1} and time.monotonic() < deadline:
            time.sleep(0.05)
            body = client.get("/healthz").json()
        assert body == {"state": "migrating", "progress": {"done": 0, "total": 1}}
        assert client.get("/healthz").status_code == 200
        assert slow_migrator.wait(timeout=30) == 0
        assert client.get("/healthz").json()["state"] == "ok"


@pytest.mark.req("ENF-OPS-002")
def test_a_second_migrate_is_refused_while_one_runs(
    settings: Settings, slow_migrator: subprocess.Popen[bytes]
) -> None:
    with TestClient(create_app(settings)) as client:
        wait_for_state(client, "migrating")
    with pytest.raises(MigrationInProgressError):
        migrate(settings)


@pytest.mark.req("ENF-OPS-002")
def test_a_killed_migrator_does_not_leave_the_state_stuck(
    settings: Settings, slow_migrator: subprocess.Popen[bytes]
) -> None:
    with TestClient(create_app(settings)) as client:
        wait_for_state(client, "migrating")
        slow_migrator.kill()
        slow_migrator.wait()
        assert client.get("/healthz").json()["state"] == "ok"
