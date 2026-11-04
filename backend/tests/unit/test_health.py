"""ENF-OPS-003: /healthz and /readyz."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import MigrationStateFile, migrate
from shelfhold.main import create_app


def test_healthz_returns_200(settings: Settings) -> None:
    migrate(settings)
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["state"] == "ok"


def test_healthz_reports_migrating_state(settings: Settings) -> None:
    migrate(settings)
    state = MigrationStateFile(settings.data_dir)
    with TestClient(create_app(settings)) as client:
        state.write(done=1, total=3)
        during = client.get("/healthz")
        state.clear()
        after = client.get("/healthz")
    assert during.status_code == 200
    assert during.json() == {"state": "migrating", "progress": {"done": 1, "total": 3}}
    assert after.json()["state"] == "ok"


def test_healthz_ignores_stale_migration_state_of_a_dead_process(settings: Settings) -> None:
    migrate(settings)
    MigrationStateFile(settings.data_dir).write(done=1, total=3, pid=2**22 + 12345)
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json()["state"] == "ok"


def test_readyz_is_200_when_migrated_and_volumes_writable(settings: Settings) -> None:
    migrate(settings)
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert all(response.json()["checks"].values())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_readyz_fails_when_db_readonly(settings: Settings) -> None:
    migrate(settings)
    os.chmod(settings.database_path, stat.S_IRUSR | stat.S_IRGRP)
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database_writable"] is False


def test_readyz_fails_when_migrations_pending(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["migrations_applied"] is False
