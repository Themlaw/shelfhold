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
from shelfhold.infrastructure.migrate import (
    MigrationInProgressError,
    MigrationStateFile,
    migrate,
)
from shelfhold.main import create_app
from tests.integration.test_migrations import scripts_with_extra_revision

RUNNER = """
import os, sys
from pathlib import Path
from shelfhold.config import load_settings
from shelfhold.infrastructure.migrate import migrate

migrate(load_settings(os.environ).values, script_location=Path(sys.argv[1]))
"""
# The slow revision waits for the test to release it, instead of a fixed sleep: a timed one raced
# the 5 s busy_timeout of the other connections and the test's own polling under load.
SLOW_REVISION = (
    "import os, time\n"
    "from pathlib import Path\n"
    "release = Path(os.environ['SHELFHOLD_TEST_RELEASE'])\n"
    "deadline = time.monotonic() + 120\n"
    "while not release.exists() and time.monotonic() < deadline:\n"
    "    time.sleep(0.05)\n"
)


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """Creating this file lets the slow migration finish."""
    return tmp_path / "release-migration"


@pytest.fixture
def slow_migrator(
    settings: Settings, env: dict[str, str], tmp_path: Path, release: Path
) -> Iterator[subprocess.Popen[bytes]]:
    migrate(settings)
    scripts = scripts_with_extra_revision(tmp_path, SLOW_REVISION)
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(runner), str(scripts)],
        env={**os.environ, **env, "SHELFHOLD_TEST_RELEASE": str(release)},
        cwd=tmp_path,
    )
    state = MigrationStateFile(settings.data_dir)
    deadline = time.monotonic() + 20
    while state.read() is None and time.monotonic() < deadline:  # the migration owns `.lock` first
        time.sleep(0.05)
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
    settings: Settings, slow_migrator: subprocess.Popen[bytes], release: Path
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
        release.touch()  # the migration may finish now
        assert slow_migrator.wait(timeout=60) == 0
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


@pytest.mark.req("ENF-OPS-015", "ENF-OPS-002")
def test_a_start_behind_a_migration_says_so_and_takes_the_lock_when_it_is_free(
    settings: Settings,
    slow_migrator: subprocess.Popen[bytes],
    release: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structlog

    from shelfhold import runtime
    from shelfhold.infrastructure.instance import InstanceLock, InstanceLockHeldError

    monkeypatch.setattr(runtime, "RETAKE_INTERVAL_S", 0.05)
    monkeypatch.setattr("shelfhold.main.RETAKE_INTERVAL_S", 0.05)
    # the slow revision of this fixture is not one of the package's: its schema looks "newer"
    monkeypatch.setattr("shelfhold.main.check_schema_not_newer", lambda _settings: None)
    with structlog.testing.capture_logs() as logs, TestClient(create_app(settings)) as client:

        def said(event: str) -> bool:
            return any(entry["event"] == event for entry in logs)

        wait_for_state(client, "migrating")
        assert said("instance.waiting_for_lock")  # not mute: the wait is logged
        release.touch()
        assert slow_migrator.wait(timeout=60) == 0
        deadline = time.monotonic() + 30
        while not said("instance.lock_taken") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert said("instance.lock_taken")
        other = InstanceLock(settings.data_dir)  # the service holds it again, and says no
        with pytest.raises(InstanceLockHeldError):
            other.acquire()
