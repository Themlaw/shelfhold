"""ENF-OPS-003: /healthz and /readyz."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app


@pytest.mark.req("ENF-OPS-003")
def test_healthz_returns_200(settings: Settings) -> None:
    migrate(settings)
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["state"] == "ok"


@pytest.mark.req("ENF-OPS-003")
def test_readyz_is_200_when_migrated_and_volumes_writable(settings: Settings) -> None:
    migrate(settings)
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert all(response.json()["checks"].values())


@pytest.mark.req("ENF-OPS-003")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_readyz_fails_when_db_readonly(settings: Settings) -> None:
    migrate(settings)
    os.chmod(settings.database_path, stat.S_IRUSR | stat.S_IRGRP)
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database_writable"] is False


@pytest.mark.req("ENF-OPS-003")
def test_readyz_fails_when_migrations_pending(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["migrations_applied"] is False
