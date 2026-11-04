"""EF-INT-001 / EF-UX-003: RFC 9457 problems, cursor pagination, idempotency, If-Match, 202."""

from datetime import timedelta
from pathlib import Path

import pytest

from tests.api_env import build_env

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
    assert "GET" in wrong_method.headers["allow"]


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
def test_idempotency_key_on_post_retained_24h(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    headers = {"Idempotency-Key": "key-0001"}
    first = env.client.post("/api/v1/things", json={"name": "a"}, headers=headers)
    assert first.status_code == 201
    assert "idempotent-replayed" not in first.headers

    env.clock[0] += timedelta(hours=23, minutes=59)
    replay = env.client.post("/api/v1/things", json={"name": "a"}, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replayed"] == "true"
    assert len(env.created) == 1  # no double effect

    # Same key, other payload: refused, never silently replayed.
    reused = env.client.post("/api/v1/things", json={"name": "b"}, headers=headers)
    assert reused.status_code == 422
    assert reused.json()["type"] == "/api/v1/problems/idempotency-key-reused"

    env.clock[0] += timedelta(minutes=2)  # now past 24 h from the first call
    again = env.client.post("/api/v1/things", json={"name": "a"}, headers=headers)
    assert again.status_code == 201
    assert again.json()["id"] != first.json()["id"]
    assert len(env.created) == 2


@pytest.mark.req("EF-INT-001")
def test_idempotency_replays_location_and_failed_requests_are_not_retained(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    headers = {"Idempotency-Key": "export-1"}
    first = env.client.post("/api/v1/exports", headers=headers)
    second = env.client.post("/api/v1/exports", headers=headers)
    assert second.status_code == 202
    assert second.headers["location"] == first.headers["location"]
    assert len(env.exports_started) == 1

    # A client error is part of the answer: replayed as is, without re-running the handler.
    bad = {"name": 1}
    one = env.client.post("/api/v1/things", json=bad, headers={"Idempotency-Key": "v-1"})
    two = env.client.post("/api/v1/things", json=bad, headers={"Idempotency-Key": "v-1"})
    assert one.status_code == two.status_code == 422
    assert two.headers["idempotent-replayed"] == "true"
    assert two.json() == one.json()


@pytest.mark.req("EF-INT-001")
def test_idempotency_does_not_retain_server_errors(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    headers = {"Idempotency-Key": "boom-1"}
    assert env.client.post("/api/v1/boom-post", headers=headers).status_code == 500
    assert env.client.post("/api/v1/boom-post", headers=headers).status_code == 500
    assert env.boom_calls == [1, 1]  # the second call ran again, it was not replayed


@pytest.mark.req("EF-INT-001")
def test_idempotency_key_is_scoped_and_validated(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    for bad in ("", "has space", "é" * 3, "k" * 256):
        response = env.client.post(
            "/api/v1/things", json={"name": "a"}, headers={"Idempotency-Key": bad}
        )
        assert response.status_code == 400, bad
        assert response.json()["type"] == "/api/v1/problems/idempotency-key-invalid"
    # Without the header nothing is retained.
    env.client.post("/api/v1/things", json={"name": "a"})
    env.client.post("/api/v1/things", json={"name": "a"})
    assert len(env.created) == 2


@pytest.mark.req("EF-INT-001")
def test_if_match_stale_returns_412(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    current = env.client.get("/api/v1/things/1")
    etag = current.headers["etag"]
    assert etag == '"1"'

    ok = env.client.put("/api/v1/things/1", json={"name": "new"}, headers={"If-Match": etag})
    assert ok.status_code == 200

    stale = env.client.put("/api/v1/things/1", json={"name": "late"}, headers={"If-Match": etag})
    assert stale.status_code == 412
    assert stale.headers["content-type"].startswith(PROBLEM_JSON)
    assert stale.json()["type"] == "/api/v1/problems/precondition-failed"
    assert env.client.get("/api/v1/things/1").json()["name"] == "new"  # untouched

    weak = env.client.put("/api/v1/things/1", json={"name": "w"}, headers={"If-Match": 'W/"2"'})
    assert weak.status_code == 412  # strong comparison only
    star = env.client.put("/api/v1/things/1", json={"name": "s"}, headers={"If-Match": "*"})
    assert star.status_code == 200
    listed = env.client.put(
        "/api/v1/things/1", json={"name": "l"}, headers={"If-Match": '"9", "3"'}
    )
    assert listed.status_code == 200  # version 3 is among the candidates

    missing = env.client.put("/api/v1/things/1", json={"name": "m"})
    assert missing.status_code == 428
    assert missing.json()["type"] == "/api/v1/problems/precondition-required"


@pytest.mark.req("EF-INT-001")
def test_long_operation_returns_202_with_job_location(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    response = env.client.post("/api/v1/exports")
    assert response.status_code == 202
    job_id = env.exports_started[0]
    assert response.headers["location"] == f"/api/v1/jobs/{job_id}"
    assert response.json() == {"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}
