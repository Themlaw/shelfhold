"""EF-INT-001 / EF-UX-003: RFC 9457 problems, cursor pagination, idempotency, If-Match, 202."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

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
from tests.api_env import T0, build_env

PROBLEM_JSON = "application/problem+json"


@pytest.mark.req("EF-INT-001", "EF-UX-003")
def test_problem_details_rfc9457_type_is_stable_code_uri(tmp_path: Path) -> None:
    """Also: test_problem_details_rfc9457."""
    env = build_env(tmp_path)
    missing = env.client.get("/api/v1/nothing-here")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith(PROBLEM_JSON)
    body = missing.json()
    assert body["type"] == "/api/v1/problems/not-found"
    assert body["code"] == "not_found"
    assert body["status"] == 404
    assert body["title"] and body["action"]  # French, from the catalog
    assert body["request_id"] == missing.headers["x-request-id"]

    wrong_method = env.client.delete("/api/v1/things")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["type"] == "/api/v1/problems/method-not-allowed"
    assert wrong_method.headers["allow"]  # the header of the underlying 405 is kept


@pytest.mark.req("EF-INT-001")
def test_validation_error_422_is_a_problem_without_echoing_input(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    response = env.client.post("/api/v1/things", json={"name": 12345, "secret": "hunter2"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["type"] == "/api/v1/problems/validation-failed"
    assert body["errors"][0]["loc"] == ["body", "name"]
    assert "hunter2" not in response.text
    assert "12345" not in response.text  # the offending input is never echoed back

    bad_limit = env.client.get("/api/v1/things", params={"limit": 0})
    assert bad_limit.status_code == 422
    assert bad_limit.json()["code"] == "validation_failed"


@pytest.mark.req("EF-INT-001", "EF-UX-003")
def test_problem_detail_carries_no_internal_detail_or_trace(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    response = env.client.get("/api/v1/boom", headers={"X-Request-ID": "req-9"})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["type"] == "/api/v1/problems/internal-error"
    assert body["request_id"] == "req-9"
    for leaked in ("/data/secret", "OperationalError", "Traceback", "RuntimeError"):
        assert leaked not in response.text
    assert "x-robots-tag" in response.headers  # still wrapped by the discretion headers


@pytest.mark.req("EF-INT-001")
def test_cursor_pagination(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = env.client.get("/api/v1/things", params=params).json()
        pages += 1
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(page["items"]) == 3
    assert seen == [1, 2, 3, 4, 5, 6, 7]
    assert pages == 3

    # A row inserted mid-walk neither duplicates nor skips (keyset, not offset).
    first = env.client.get("/api/v1/things", params={"limit": 2}).json()
    env.client.post("/api/v1/things", json={"name": "late"})
    rest = env.client.get(
        "/api/v1/things", params={"limit": 50, "cursor": first["next_cursor"]}
    ).json()
    assert [i["id"] for i in rest["items"]] == [3, 4, 5, 6, 7, 8]


@pytest.mark.req("EF-INT-001")
@pytest.mark.parametrize("cursor", ["!!!", "bm90LWpzb24", "WzEsWzJdXQ", "x" * 600])
def test_invalid_cursor_is_a_400_problem(tmp_path: Path, cursor: str) -> None:
    env = build_env(tmp_path)
    response = env.client.get("/api/v1/things", params={"cursor": cursor})
    assert response.status_code == 400
    assert response.json()["type"] == "/api/v1/problems/invalid-cursor"


@pytest.mark.req("EF-INT-001")
def test_unknown_api_path_under_the_spa_is_a_404_problem(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("<!doctype html><title>Shelfhold</title>", encoding="utf-8")
    env = build_env(tmp_path, frontend_dir=build)
    for path in ("/api/v1/nothing-here", "/api", "/healthz/x"):
        response = env.client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert response.json()["type"] == "/api/v1/problems/not-found"
    assert env.client.get("/some/client/route").text.startswith("<!doctype html>")


@pytest.mark.req("EF-INT-001")
def test_non_api_405_stays_plain(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("<!doctype html><title>Shelfhold</title>", encoding="utf-8")
    env = build_env(tmp_path, frontend_dir=build)
    response = env.client.post("/some/client/route")
    assert response.status_code == 405
    assert not response.headers["content-type"].startswith(PROBLEM_JSON)


@pytest.mark.req("EF-INT-001")
def test_idempotency_requires_an_actor_resolver(tmp_path: Path) -> None:
    from shelfhold.interfaces.http_api.app import create_http_app
    from tests.api_env import _Health

    engine = create_db_engine(tmp_path / "x.db")
    with pytest.raises(ValueError, match="idempotency_requires_actor_of"):
        create_http_app(
            health=_Health(),
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
        encode_cursor(["text"]),  # the consumer issues one integer
        encode_cursor([1, 2]),
    ],
)
def test_cursor_range_layout_and_empty_are_refused(tmp_path: Path, cursor: str) -> None:
    env = build_env(tmp_path)
    response = env.client.get("/api/v1/things", params={"cursor": cursor})
    assert response.status_code == 400
    assert response.json()["type"] == "/api/v1/problems/invalid-cursor"


@pytest.mark.req("EF-INT-001")
def test_etag_value_refuses_a_comma() -> None:
    with pytest.raises(ValueError, match="invalid_etag_value"):
        etag_for_version("1,2")
    assert etag_for_version("v-1.2") == '"v-1.2"'
