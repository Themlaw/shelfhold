"""M3 L49, EF-INT-017 / EF-INT-018: what the admin's queue screen reads and the doors it uses.

The screen opens no door of its own: it reads `GET /jobs` and `GET /maintenance`, and writes through
the routes of L13. Every refusal that the screen relies on is tried here against the real app."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from tests.auth_env import HTTPS, AuthEnv, auth_env

JOBS = "/api/v1/jobs"
MAINTENANCE = "/api/v1/maintenance"
WRITES = (
    ("POST", "/api/v1/maintenance/enter", None),
    ("POST", "/api/v1/maintenance/exit", None),
    ("POST", "/api/v1/queue/pause", {"source": None}),
    ("POST", "/api/v1/queue/resume", {"source": None}),
)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200
        yield env


def _seed(settings: Settings, **values: object) -> str:
    job_id = new_ulid()
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.insert(platform.job_view).values(
                    id=job_id, kind="fetch.chapter", status="queued", dedupe_key=job_id, **values
                )
            )
    finally:
        engine.dispose()
    return job_id


@pytest.mark.req("EF-INT-017")
def test_job_list_says_for_whom_which_source_and_which_priority(api: AuthEnv) -> None:
    request = new_ulid()
    job = _seed(api.settings, request_id=request, source_key="alpha", priority_class="request")
    other = _seed(api.settings)
    items = {i["id"]: i for i in api.client.get(JOBS).json()["items"]}
    assert items[job]["request_id"] == request
    assert items[job]["source_key"] == "alpha"
    assert items[job]["priority_class"] == "request"
    assert items[other]["request_id"] is None and items[other]["priority_class"] == "new"
    assert "progress" in items[job]


@pytest.mark.req("EF-INT-017")
def test_the_queue_screen_reads_are_refused_without_a_session(api: AuthEnv) -> None:
    request = new_ulid()
    _seed(api.settings, request_id=request, source_key="alpha")
    # not entered (`with`): a second lifespan would start a second set of Huey workers on the
    # app that `auth_env` already runs, and the test would leave them behind
    anonymous = TestClient(api.app, base_url=HTTPS)  # type: ignore[arg-type]
    for path in (JOBS, MAINTENANCE):
        response = anonymous.get(path)
        assert response.status_code == 401, path
        assert request not in response.text and "alpha" not in response.text


@pytest.mark.req("EF-INT-017", "EF-INT-018")
def test_the_queue_screen_writes_are_refused_without_a_session_or_a_csrf_token(
    api: AuthEnv,
) -> None:
    job = _seed(api.settings)
    paths = [*WRITES, ("POST", f"/api/v1/jobs/{job}/cancel", None)]
    # not entered (`with`): a second lifespan would start a second set of Huey workers on the
    # app that `auth_env` already runs, and the test would leave them behind
    anonymous = TestClient(api.app, base_url=HTTPS)  # type: ignore[arg-type]
    for _method, path, body in paths:
        assert anonymous.post(path, json=body).status_code in (401, 403), path
    for _method, path, body in paths:  # signed in, but no CSRF token: still refused
        assert api.client.post(path, json=body).status_code in (401, 403), path
    status = api.client.get(MAINTENANCE).json()
    assert status["maintenance"]["active"] is False and status["queue"]["paused"] == []


@pytest.mark.req("EF-INT-018")
def test_while_the_mode_is_on_the_screen_can_only_leave_it(api: AuthEnv) -> None:
    job = _seed(api.settings)
    assert api.post("/api/v1/maintenance/enter").status_code == 200
    assert api.client.get(MAINTENANCE).json()["maintenance"]["active"] is True
    for path, body in (
        ("/api/v1/queue/pause", {"source": None}),
        ("/api/v1/queue/resume", {"source": None}),
        (f"/api/v1/jobs/{job}/cancel", None),
        (f"/api/v1/jobs/{job}/priority", {"priority_class": "backfill"}),
        (f"/api/v1/jobs/{job}/replay", None),
    ):
        refused = api.post(path, json=body)
        assert refused.status_code == 503, path
        assert refused.json()["code"] == "maintenance.active"
    assert api.client.get(JOBS).json()["items"][0]["status"] == "queued"
    assert api.post("/api/v1/maintenance/exit").status_code == 200
    assert api.client.get(MAINTENANCE).json()["maintenance"]["active"] is False
