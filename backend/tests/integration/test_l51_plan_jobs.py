"""L51 (EF-INT-001): a long operation answers 202 + `Location`, the job is polled at
`GET /jobs/{id}`, and the conventions of the API (idempotency key kept 24 h, `If-Match`) are
proved on the real route `POST /plans/{id}/apply` of `create_app()`, over a real instance."""

import re
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.interfaces.http_api.idempotency import IdempotencyMiddleware
from tests.auth_env import PASSWORD, run_cli
from tests.l51_env import Live, live

pytestmark = pytest.mark.req("EF-INT-001")
ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def jobs_of(env: Live, kind: str = "export.apply") -> list[sa.Row[tuple[object, ...]]]:
    return [r for r in env.rows(platform.job_view) if r.kind == kind]


def preview(api: Live) -> tuple[str, str]:
    response = api.preview()
    assert response.status_code == 201, response.text
    return response.json()["plan_id"], response.headers["etag"]


def test_long_operation_returns_202_with_job_location(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        plan_id, etag = preview(api)
        assert api.env.exported_files() == []  # a preview writes nothing
        response = api.apply(plan_id, etag)
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert ULID.match(job_id)
        assert response.headers["location"] == f"/api/v1/jobs/{job_id}"
        assert response.json()["status_url"] == f"/api/v1/jobs/{job_id}"
        job = api.wait_job(job_id)  # GET /jobs/{id} until the end
        assert (job["id"], job["kind"], job["status"]) == (job_id, "export.apply", "succeeded")
        assert job["error_code"] is None
        assert len(api.env.exported_files()) == 2
        plans = {r.plan_id: r.status for r in api.rows(coherence.export_plan)}
        assert plans[plan_id] == "done"


def test_get_job_is_for_admins_and_unknown_jobs_are_404(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        plan_id, etag = preview(api)
        job_id = api.apply(plan_id, etag).json()["job_id"]
        anonymous = TestClient(api.api.app, base_url="https://testserver")  # type: ignore[arg-type]
        assert anonymous.get(f"/api/v1/jobs/{job_id}").status_code == 401
        assert api.client.get("/api/v1/jobs/01JNOSUCHJOB000000000000A").status_code == 404
        assert api.client.get("/api/v1/jobs/not-a-job").status_code == 404
        api.wait_job(job_id)


def test_idempotency_key_on_post_retained_24h(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        plan_id, etag = preview(api)
        key = {"Idempotency-Key": "apply-0001"}
        first = api.apply(plan_id, etag, **key)
        assert first.status_code == 202
        assert "idempotent-replayed" not in first.headers
        api.wait_job(first.json()["job_id"])

        api.api.clock.advance(timedelta(hours=23, minutes=59))
        assert api.api.login().status_code == 200  # the session did not live that long
        replay = api.apply(plan_id, etag, **key)
        assert replay.status_code == 202
        assert replay.json() == first.json()
        assert replay.headers["location"] == first.headers["location"]
        assert replay.headers["idempotent-replayed"] == "true"
        assert len(jobs_of(api)) == 1  # no double effect

        # Same key, other request: refused, never silently replayed.
        other = api.apply("01JOTHERPLAN00000000000AA", etag, **key)
        assert other.status_code == 422
        assert other.json()["type"] == "/api/v1/problems/idempotency-key-reused"

        api.api.clock.advance(timedelta(minutes=2))  # past 24 h from the first call
        assert api.api.login().status_code == 200
        again = api.apply(plan_id, etag, **key)
        assert "idempotent-replayed" not in again.headers  # the key is forgotten: it ran again
        assert (again.status_code, again.json()["code"]) == (412, "plan.stale")  # already applied
        assert len(jobs_of(api)) == 1


def test_if_match_stale_returns_412(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        plan_id, etag = preview(api)
        api.env.add_chapter(api.series, "3")  # the library moved after the preview
        stale = api.apply(plan_id, etag)
        assert stale.status_code == 412
        assert stale.headers["content-type"].startswith("application/problem+json")
        assert stale.json()["type"] == "/api/v1/problems/plan-stale"
        assert jobs_of(api) == []  # refused before any job exists
        assert api.env.exported_files() == []  # and nothing was written

        weak = api.apply(plan_id, f"W/{etag}")
        assert weak.status_code == 412  # strong comparison only
        assert weak.json()["type"] == "/api/v1/problems/precondition-failed"
        star = api.apply(plan_id, "*")
        assert star.status_code == 412  # a plan is applied only by someone who saw its version
        missing = api.apply(plan_id, None)
        assert missing.status_code == 428
        assert missing.json()["type"] == "/api/v1/problems/precondition-required"

        fresh_id, fresh_etag = preview(api)  # the way out: a new preview, then it goes through
        done = api.apply(fresh_id, fresh_etag)
        assert done.status_code == 202
        assert api.wait_job(done.json()["job_id"])["status"] == "succeeded"
        assert len(api.env.exported_files()) == 3


def test_a_plan_that_is_not_known_is_a_404_before_any_job(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        _plan_id, etag = preview(api)
        unknown = api.apply("01JNOSUCHPLAN00000000000A", etag)
        assert unknown.status_code == 404
        assert jobs_of(api) == []


def test_idempotency_replays_the_location_and_keeps_client_errors(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        plan_id, etag = preview(api)
        headers = {"Idempotency-Key": "export-1"}
        first = api.apply(plan_id, etag, **headers)
        second = api.apply(plan_id, etag, **headers)
        assert second.status_code == 202
        assert second.headers["location"] == first.headers["location"]
        assert len(jobs_of(api)) == 1
        api.wait_job(first.json()["job_id"])

        # A client error is part of the answer: replayed as is, the handler does not run again.
        bad = {"Idempotency-Key": "v-1"}
        one = api.api.post("/api/v1/plans", json={"shelf_destination_id": 1}, headers=bad)
        two = api.api.post("/api/v1/plans", json={"shelf_destination_id": 1}, headers=bad)
        assert one.status_code == two.status_code == 422
        assert two.headers["idempotent-replayed"] == "true"
        assert two.json() == one.json()


def test_idempotency_does_not_retain_server_errors(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    import shelfhold.runtime_jobs as rj

    calls: list[int] = []

    def boom(_self: object, _destination_id: str) -> None:
        calls.append(1)
        raise RuntimeError("/data/secret/path leaked sqlite3.OperationalError")

    with live(monkeypatch, settings, tmp_path) as api:
        monkeypatch.setattr(rj.PlanService, "preview", boom)
        headers = {"Idempotency-Key": "boom-1"}
        for _ in range(2):
            failed = api.preview(**headers)
            assert failed.status_code == 500
            assert "secret" not in failed.text and "sqlite3" not in failed.text
        assert calls == [1, 1]  # the second call ran again, it was not replayed


def test_idempotency_key_is_scoped_and_validated(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        for bad in ("", "has space", "k" * 256):
            response = api.preview(**{"Idempotency-Key": bad})
            assert response.status_code == 400, bad
            assert response.json()["type"] == "/api/v1/problems/idempotency-key-invalid"
        before = len(api.rows(coherence.export_plan))
        api.preview()
        api.preview()  # without the header nothing is retained: two plans
        assert len(api.rows(coherence.export_plan)) == before + 2


def test_idempotency_key_is_scoped_to_the_actor(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path
) -> None:
    with live(monkeypatch, settings, tmp_path) as api:
        secret = settings.data_dir.parent / "bob.password"
        secret.write_text(PASSWORD + "\n", encoding="utf-8")
        secret.chmod(0o600)
        assert run_cli(
            monkeypatch, settings, "admin", "create", "--username", "bob",
            "--password-file", str(secret),
        ) == 0  # fmt: skip
        bob = api.api.other_client("10.0.0.7")
        assert api.api.login("bob", client=bob).status_code == 200
        key = {"Idempotency-Key": "shared-key"}
        body = {"shelf_destination_id": api.env.destination_id}
        alice_plan = api.api.post("/api/v1/plans", json=body, headers=key)
        bob_plan = api.api.post("/api/v1/plans", json=body, headers=key, client=bob)
        assert "idempotent-replayed" not in bob_plan.headers
        assert alice_plan.json()["plan_id"] != bob_plan.json()["plan_id"]
        again = api.api.post("/api/v1/plans", json=body, headers=key)
        assert again.json() == alice_plan.json()


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 502, 503])
def test_idempotency_stores_only_deterministic_answers(status: int) -> None:
    storable = IdempotencyMiddleware._storable
    assert not storable(status, Headers({}), False, b"{}")
    for kept in (200, 201, 202, 404, 409, 412, 422):
        assert storable(kept, Headers({"content-type": "application/json"}), False, b"{}")
