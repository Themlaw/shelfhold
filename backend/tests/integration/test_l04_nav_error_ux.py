"""L04 (M3, EF-UX-005, EF-UX-003): navigation by role decided by the server, and the `cause` and
`action` of every problem+json.

The attacks: a child or a member asking for the admin navigation (or for an admin route), a client
naming its own role, a key or no session on `/api/v1/me`, a path, a host name or a secret reaching
the `cause` of a problem, and an error with no recognised code losing its request id.
"""

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.i18n import load_catalog
from shelfhold.interfaces.http_api.catalog import all_error_codes
from shelfhold.interfaces.http_api.problems import cause_of, problem_body
from tests.auth_env import ADMIN, CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows, run_cli

HTTPS = "https://testserver"
PROBLEM_JSON = "application/problem+json"
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
FAMILY = ["home", "search", "requests"]
ADMIN_ONLY = ["approve", "activity", "settings"]
PROFILES = "/api/v1/profiles"
LEAKS = ("/srv/library/secret-folder", "kavita.internal.lan", "10.0.3.7", "tok_live_0123456789")


def _as(env: AuthEnv, username: str) -> None:
    env.client.cookies.clear()
    assert env.login(username).status_code == 200


def _auth(env: AuthEnv) -> Any:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined]


# -- EF-UX-005: the navigation comes from the server, by role ------------------------------------


@pytest.mark.req("EF-UX-005")
def test_nav_by_role(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    """The admin is offered the family tabs plus To approve, Activity and Settings; a member and a
    child get the family tabs and nothing of the admin ones."""
    with auth_env(monkeypatch, settings) as api:
        _auth(api).profile_add("mum", PASSWORD, "member")
        _auth(api).profile_add("kid", PASSWORD, "child", max_age_rating=10)

        _as(api, ADMIN)
        me = api.client.get("/api/v1/me")
        assert me.status_code == 200
        assert me.json()["role"] == "admin"
        assert me.json()["username"] == ADMIN
        assert me.json()["nav"] == [*FAMILY, *ADMIN_ONLY]

        for username, role in (("mum", "member"), ("kid", "child")):
            _as(api, username)
            body = api.client.get("/api/v1/me").json()
            assert body["role"] == role
            assert body["nav"] == FAMILY  # exactly the family tabs, in order
            assert not set(ADMIN_ONLY) & set(body["nav"])


@pytest.mark.req("EF-UX-005", "ENF-SEC-010")
def test_a_client_cannot_name_its_own_role_or_navigation(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """The role is the one of the session: a query, a header or a cookie that claims another one
    changes nothing."""
    with auth_env(monkeypatch, settings) as api:
        _auth(api).profile_add("kid", PASSWORD, "child", max_age_rating=10)
        _as(api, "kid")
        forged = api.client.get(
            "/api/v1/me?role=admin&nav=activity,settings",
            headers={
                "X-Role": "admin",
                "X-Shelfhold-Role": "admin",
                "X-Forwarded-User": ADMIN,
            },
        )
        assert forged.status_code == 200
        assert forged.json()["role"] == "child"
        assert forged.json()["nav"] == FAMILY


@pytest.mark.req("EF-UX-005", "ENF-SEC-010")
def test_the_admin_destinations_are_refused_to_a_member_and_a_child_by_the_server(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Hiding the tabs is not the control: the admin routes behind them answer 403 and write
    nothing, whatever the screen shows."""
    with auth_env(monkeypatch, settings) as api:
        _auth(api).profile_add("mum", PASSWORD, "member")
        _auth(api).profile_add("kid", PASSWORD, "child", max_age_rating=10)
        before = db_rows(api.settings, 'SELECT username, role FROM "user" ORDER BY id')
        body = {"username": "x", "password": PASSWORD, "role": "admin"}
        for username in ("mum", "kid"):
            _as(api, username)
            assert api.client.get(PROFILES).status_code == 403, username
            created = api.client.post(PROFILES, json=body, headers={CSRF_HEADER: api.csrf()})
            assert created.status_code == 403, username
            assert created.json()["type"] == "/api/v1/problems/forbidden"
        assert db_rows(api.settings, 'SELECT username, role FROM "user" ORDER BY id') == before


@pytest.mark.req("EF-UX-005", "ENF-SEC-010")
def test_me_needs_a_session_not_a_key_and_not_nobody(
    monkeypatch: pytest.MonkeyPatch, settings: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    with auth_env(monkeypatch, settings) as api:
        assert api.client.get("/api/v1/me").status_code == 401  # nobody: no navigation at all
        assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "k") == 0
        key = KEY.search(capsys.readouterr().out)
        assert key
        keyed = api.client.get("/api/v1/me", headers={"Authorization": f"Bearer {key.group(0)}"})
        assert keyed.status_code == 403
        assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
        assert "nav" not in keyed.json()


@pytest.mark.req("EF-UX-005")
def test_an_unknown_role_is_offered_nothing() -> None:
    from shelfhold.interfaces.http_api.me import navigation_for

    assert navigation_for("integration") == []
    assert navigation_for("") == []
    assert navigation_for("Admin") == []  # the match is exact, never case-folded
    assert navigation_for("admin")[-3:] == ADMIN_ONLY


# -- EF-UX-003: a problem carries a stable code, a text, a cause and an action --------------------


@pytest.mark.req("EF-UX-003")
def test_problem_card_shows_cause_and_action(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """The body the error component shows: stable code, text, cause, action, request id."""
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200
        response = api.client.get("/api/v1/nothing-here")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        body = response.json()
        catalog = load_catalog()
        assert body["type"] == "/api/v1/problems/not-found"
        assert body["code"] == "not_found"
        assert body["title"] == catalog["errors.not_found.title"]
        assert body["action"] == catalog["errors.not_found.action"]
        assert body["cause"] == catalog["errors.not_found.cause"]
        assert body["request_id"] == response.headers["x-request-id"]


@pytest.mark.req("EF-UX-003")
def test_every_code_has_a_cause_from_the_closed_catalog_vocabulary() -> None:
    """Each code answers with its own sentence or the generic one of its audience, nothing else."""
    catalog = load_catalog()
    allowed = {v for k, v in catalog.items() if k.endswith(".cause") or ".cause_default." in k}
    for spec in all_error_codes():
        cause = cause_of(spec)
        assert cause.strip() and cause in allowed, spec.code
        assert problem_body(spec)["cause"] == cause
    assert {catalog["errors.cause_default.user"], catalog["errors.cause_default.admin"]} <= allowed


@pytest.mark.req("EF-UX-003", "ENF-SEC-001")
def test_a_cause_text_of_the_catalog_names_no_path_host_address_or_placeholder() -> None:
    catalog = load_catalog()
    texts = {k: v for k, v in catalog.items() if k.endswith(".cause") or ".cause_default." in k}
    assert len(texts) >= 14
    leak = r"[{}]|https?:|\w/\w|\\|\b\d{1,3}(\.\d{1,3}){3}\b|\.(lan|local)\b"
    for key, text in texts.items():
        assert not re.search(leak, text), key


@pytest.mark.req("EF-UX-003", "ENF-SEC-001")
def test_a_cause_never_carries_what_the_raising_code_puts_in_detail_or_extensions() -> None:
    """A path, a host name, an address and a token in `detail` or in an extension never reach
    `cause` (nor `action` or `title`)."""
    dirty = " ".join(LEAKS)
    for spec in all_error_codes():
        body = problem_body(spec, detail=dirty, extensions={"why": dirty})
        for member in ("cause", "action", "title"):
            assert not any(leak in body[member] for leak in LEAKS), (spec.code, member)
        assert body["cause"] == cause_of(spec)


@pytest.mark.req("EF-UX-003")
def test_unknown_error_code_shows_request_id(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """An unexpected failure has no catalog code of its own: it answers the generic internal
    problem, with a cause and an action, and always the request id a person can report."""
    with auth_env(monkeypatch, settings) as api:
        # not entered (`with`): a second lifespan would start a second set of Huey workers on the
        # app that `auth_env` already runs, and the test would leave them behind
        client = TestClient(api.app, base_url=HTTPS, raise_server_exceptions=False)  # type: ignore[arg-type]
        assert api.login(client=client).status_code == 200
        from shelfhold.interfaces.http_api import me as me_module

        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError(" ".join(LEAKS))

        monkeypatch.setattr(me_module, "navigation_for", boom)
        response = client.get("/api/v1/me", headers={"X-Request-ID": "req-unknown-1"})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["action"] and body["cause"]
    assert body["request_id"] == "req-unknown-1"
    assert not any(leak in response.text for leak in LEAKS)  # the exception message stays inside
