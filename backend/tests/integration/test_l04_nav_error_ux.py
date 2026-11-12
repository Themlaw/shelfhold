"""L04 (M3, EF-UX-005, EF-UX-003): role-based navigation source and problem+json UX.

A problem carries a stable code, a text, a cause and an action; an unknown code still exposes the
request identifier so the person can report it.
"""

import pytest
from fastapi.testclient import TestClient

from tests.api_env import T0  # noqa: F401 - the clock origin the fixtures share
from tests.auth_env import HTTPS, auth_env

PROBLEM_JSON = "application/problem+json"


@pytest.mark.req("EF-UX-005")
def test_nav_by_role(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    """The role that drives the family/admin navigation comes from GET /api/v1/me."""
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200

        me = api.client.get("/api/v1/me")
        assert me.status_code == 200
        body = me.json()
        assert body["role"] == "admin"
        assert body["username"] == "admin"
        assert body["id"]


@pytest.mark.req("EF-UX-003")
def test_problem_card_shows_cause_and_action(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    """A problem+json body carries a stable code, a text, a cause and an action."""
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200

        response = api.client.get("/api/v1/nothing-here")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        body = response.json()
        assert body["type"] == "/api/v1/problems/not-found"
        assert body["code"] == "not_found"
        assert body["title"]
        assert body["action"]
        assert body["cause"]  # the probable cause, surfaced by the error component
        assert body["request_id"] == response.headers["x-request-id"]


@pytest.mark.req("EF-UX-003")
def test_unknown_error_code_shows_request_id(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    """An error with no recognised stable code still shows the request identifier."""
    with (
        auth_env(monkeypatch, settings) as api,
        TestClient(
            api.app,  # type: ignore[arg-type]
            base_url=HTTPS,
            raise_server_exceptions=False,
        ) as client,
    ):
        assert api.login(client=client).status_code == 200
        # Force the global handler onto an unexpected failure: the resulting internal error is the
        # generic fallback and must always carry the request id so a person can report it.
        from shelfhold.interfaces.http_api import identity as identity_module

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(identity_module, "MeOut", boom)
        response = client.get("/api/v1/me", headers={"X-Request-ID": "req-unknown-1"})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["action"]
    assert body["request_id"] == "req-unknown-1"
