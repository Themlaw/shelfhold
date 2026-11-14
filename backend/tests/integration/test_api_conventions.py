"""EF-INT-001 / EF-UX-003: RFC 9457 problems, cursor pagination, idempotency, If-Match, 202."""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.idempotency_store import (
    IN_FLIGHT_LEASE,
    Fresh,
    IdempotencyStore,
    InFlight,
    Replay,
)
from shelfhold.infrastructure.schema import metadata
from shelfhold.interfaces.http_api.pagination import encode_cursor
from shelfhold.interfaces.http_api.preconditions import etag_for_version
from tests.api_env import T0, Health
from tests.auth_env import CSRF_HEADER, HTTPS, AuthEnv, auth_env
from tests.ops_env import add_job

PROBLEM_JSON = "application/problem+json"
JOBS = "/api/v1/jobs"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    """`create_app()` as composed, an administrator signed in: the conventions are proved on the
    real routes (the job list), never on a route that exists only in a test."""
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200
        yield env


def _seed_jobs(settings: Settings, count: int) -> list[str]:
    engine = create_db_engine(settings.database_path)
    try:
        return [add_job(engine, kind="export.run", status="succeeded") for _ in range(count)]
    finally:
        engine.dispose()


@pytest.mark.req("EF-INT-001", "EF-UX-003")
def test_problem_details_rfc9457_type_is_stable_code_uri(api: AuthEnv) -> None:
    """Also: test_problem_details_rfc9457."""
    missing = api.client.get("/api/v1/nothing-here")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith(PROBLEM_JSON)
    body = missing.json()
    assert body["type"] == "/api/v1/problems/not-found"
    assert body["code"] == "not_found"
    assert body["status"] == 404
    assert body["title"] and body["action"]  # French, from the catalog
    assert body["request_id"] == missing.headers["x-request-id"]

    wrong_method = api.client.delete(JOBS, headers={CSRF_HEADER: api.csrf()})
    assert wrong_method.status_code == 405
    assert wrong_method.json()["type"] == "/api/v1/problems/method-not-allowed"
    assert wrong_method.headers["allow"]  # the header of the underlying 405 is kept


@pytest.mark.req("EF-INT-001")
def test_validation_error_422_is_a_problem_without_echoing_input(api: AuthEnv) -> None:
    response = api.client.post(
        "/api/v1/auth/login", json={"username": 12345, "password": "hunter2", "secret": "hunter2"}
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["type"] == "/api/v1/problems/validation-failed"
    assert body["errors"][0]["loc"] == ["body", "username"]
    assert "hunter2" not in response.text
    assert "12345" not in response.text  # the offending input is never echoed back

    bad_limit = api.client.get(JOBS, params={"limit": 0})
    assert bad_limit.status_code == 422
    assert bad_limit.json()["code"] == "validation_failed"


class _Broken:
    """An operating service whose database fails with a message that names a path."""

    def jobs_list(self, **_: object) -> list[object]:
        raise RuntimeError("/data/secret/path leaked sqlite3.OperationalError")


@pytest.mark.req("EF-INT-001", "EF-UX-003")
def test_problem_detail_carries_no_internal_detail_or_trace(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings, ops_services=_Broken()) as api:
        # not entered (`with`): a second lifespan would start a second set of Huey workers on the
        # app that `auth_env` already runs, and the test would leave them behind
        client = TestClient(
            api.app,  # type: ignore[arg-type]
            base_url=HTTPS,
            raise_server_exceptions=False,
        )
        assert api.login(client=client).status_code == 200
        response = client.get(JOBS, headers={"X-Request-ID": "req-9"})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["type"] == "/api/v1/problems/internal-error"
    assert body["request_id"] == "req-9"
    for leaked in ("/data/secret", "OperationalError", "Traceback", "RuntimeError"):
        assert leaked not in response.text
    assert "x-robots-tag" in response.headers  # still wrapped by the discretion headers


@pytest.mark.req("EF-INT-001")
def test_cursor_pagination_on_the_job_list(api: AuthEnv) -> None:
    ids = _seed_jobs(api.settings, 7)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = api.client.get(JOBS, params=params).json()
        pages += 1
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(page["items"]) == 3
    assert set(ids) <= set(seen) and len(set(seen)) == len(seen)  # none repeated, none skipped
    assert pages >= 3

    # A row inserted mid-walk neither duplicates nor skips (keyset, not offset).
    first = api.client.get(JOBS, params={"limit": 2}).json()
    _seed_jobs(api.settings, 1)
    rest = api.client.get(JOBS, params={"limit": 50, "cursor": first["next_cursor"]}).json()
    served = {i["id"] for i in first["items"]}
    assert served.isdisjoint(i["id"] for i in rest["items"])
    assert set(seen) - served <= {i["id"] for i in rest["items"]}


@pytest.mark.req("EF-INT-001")
def test_job_list_has_an_etag_and_answers_304(api: AuthEnv) -> None:
    _seed_jobs(api.settings, 2)
    first = api.client.get(JOBS)
    etag = first.headers["etag"]
    unchanged = api.client.get(JOBS, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304 and unchanged.content == b""
    _seed_jobs(api.settings, 1)
    assert api.client.get(JOBS, headers={"If-None-Match": etag}).status_code == 200


@pytest.mark.req("EF-INT-001")
@pytest.mark.parametrize("cursor", ["!!!", "bm90LWpzb24", "WzEsWzJdXQ", "x" * 600])
def test_invalid_cursor_is_a_400_problem(api: AuthEnv, cursor: str) -> None:
    response = api.client.get(JOBS, params={"cursor": cursor})
    assert response.status_code == 400
    assert response.json()["type"] == "/api/v1/problems/invalid-cursor"


def _spa(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    build.mkdir()
    csp = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'"
    page = (
        f'<!doctype html><meta http-equiv="content-security-policy" content="{csp}">'
        "<title>Shelfhold</title>"
    )
    (build / "index.html").write_text(page, encoding="utf-8")  # a valid build: it carries its CSP
    return build


@pytest.mark.req("EF-INT-001")
def test_unknown_api_path_under_the_spa_is_a_404_problem(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    served = settings.model_copy(update={"frontend_dir": _spa(tmp_path)})
    with auth_env(monkeypatch, served) as api:
        for path in ("/api/v1/nothing-here", "/api", "/healthz/x"):
            response = api.client.get(path)
            assert response.status_code == 404, path
            assert response.headers["content-type"].startswith(PROBLEM_JSON)
            assert response.json()["type"] == "/api/v1/problems/not-found"
        assert api.client.get("/some/client/route").text.startswith("<!doctype html>")


@pytest.mark.req("EF-INT-001")
def test_non_api_405_stays_plain(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    served = settings.model_copy(update={"frontend_dir": _spa(tmp_path)})
    with auth_env(monkeypatch, served) as api:
        response = api.client.post("/some/client/route")
    assert response.status_code == 405
    assert not response.headers["content-type"].startswith(PROBLEM_JSON)


@pytest.mark.req("EF-INT-001")
def test_idempotency_requires_an_actor_resolver(tmp_path: Path) -> None:
    from shelfhold.interfaces.http_api.app import create_http_app

    engine = create_db_engine(tmp_path / "x.db")
    with pytest.raises(ValueError, match="idempotency_requires_actor_of"):
        create_http_app(
            health=Health(),
            on_startup=lambda: None,
            version="0",
            idempotency=IdempotencyStore(engine),
        )


@pytest.mark.req("EF-INT-001")
def test_lost_idempotency_claim_does_not_overwrite_the_new_owner(tmp_path: Path) -> None:
    engine = create_db_engine(tmp_path / "claims.db")
    metadata.create_all(engine)
    now = [T0]
    store = IdempotencyStore(engine, clock=lambda: now[0])
    args = {
        "actor_id": "a",
        "key": "k",
        "method": "POST",
        "path": "/p",
        "request_hash": "sha256:" + "a" * 64,
    }
    first = store.begin(**args)
    assert isinstance(first, Fresh)
    now[0] += IN_FLIGHT_LEASE + timedelta(seconds=1)  # the lease of the first one lapses
    second = store.begin(**args)  # same request retried: the key is granted again
    assert isinstance(second, Fresh)

    assert store.complete(first.claim, status=201, headers={}, body=b'{"n":1}') is False
    store.release(first.claim)  # nor can it delete the new owner's reservation
    assert isinstance(store.begin(**args), InFlight)
    assert store.complete(second.claim, status=201, headers={}, body=b'{"n":2}') is True
    replay = store.begin(**args)
    assert isinstance(replay, Replay)
    assert json.loads(replay.body) == {"n": 2}


@pytest.mark.req("EF-INT-001")
@pytest.mark.parametrize(
    "cursor",
    [
        "",  # empty is malformed, not "absent"
        encode_cursor([2**63]),
        encode_cursor([-(2**63) - 1]),
        encode_cursor([1]),  # the job list issues one text key: the id
        encode_cursor(["a", "b"]),
    ],
)
def test_cursor_range_layout_and_empty_are_refused(api: AuthEnv, cursor: str) -> None:
    response = api.client.get(JOBS, params={"cursor": cursor})
    assert response.status_code == 400
    assert response.json()["type"] == "/api/v1/problems/invalid-cursor"


@pytest.mark.req("EF-INT-001")
def test_etag_value_refuses_a_comma() -> None:
    with pytest.raises(ValueError, match="invalid_etag_value"):
        etag_for_version("1,2")
    assert etag_for_version("v-1.2") == '"v-1.2"'
