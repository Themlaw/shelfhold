"""SEC-1 (EF-USR-024, ENF-SEC-010): a read key of the administrator reads as her profile, and no
more. The administrator's surfaces (what a member's session is refused) are for her session or a
key of scope `admin` that may write: a read-only key handed to a program never opens them.

The meta-test walks every GET route of the OpenAPI document with a member's session and with a read
key of the admin: a route that refuses the member refuses the key, unless it is listed below with
the reason the key must read it. A route added under a guard that only tests the role fails here.
Only GET is walked: a read-only key is refused every other method before any guard
(`require_principal`), which `test_a_read_key_writes_nothing` keeps.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import PASSWORD, AuthEnv, auth_env, run_cli

pytestmark = pytest.mark.req("EF-USR-024", "ENF-SEC-010")

ULID = "01HZZZZZZZZZZZZZZZZZZZZZZZ"

# Routes a member's session is refused (403) that a read key of the admin must still read, each
# with its reason. None today: every such route is a surface of the administrator (the profiles'
# overview, the statistics, the configuration, the plugins, the queues, a search that asks the
# sources...), and a program holding her read key reads what her profile reads, not what her role
# administers. A route added here needs a reason a reviewer agrees with.
ADMIN_SURFACES_A_READ_KEY_READS: dict[str, str] = {}

# What the read key of the admin must keep reading: her profile's view of the library. The routes
# of one's own account (`/me`, sessions, passkeys) are a session's alone, for any key (unchanged).
READ_KEY_READS: dict[str, str] = {
    "/api/v1/works": "the catalogue, filtered by her profile like any profile's",
    "/api/v1/collections": "her collections and the shared ones, as her profile sees them",
    "/api/v1/selections": "her saved selections",
    "/api/v1/activity": "the activity, read as the integration role (never her payloads)",
    "/api/v1/auth/session": "who the key is: the program checks its credential",
    "/api/v1/about": "the version, as for anyone signed in",
}

# Surfaces the audit found open to the key (a sample): the meta-test must keep walking them.
AUDITED = (
    "/api/v1/profiles/overview",
    "/api/v1/stats",
    "/api/v1/config",
    "/api/v1/plugins",
    "/api/v1/plugin-repos",
    "/api/v1/devices/connect",
    "/api/v1/audit/komga-accounts",
    "/api/v1/sources/{source_key}/search",
    "/api/v1/sources/{source_key}/series/{remote_id}/estimate",
)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        auth = env.app.state.auth_gate.auth  # type: ignore[attr-defined]
        auth.profile_add("member", PASSWORD, "member")
        yield env


def _member(api: AuthEnv) -> Any:
    client = api.other_client("10.0.0.20")
    assert api.login("member", client=client).status_code == 200
    return client


def _key(api: AuthEnv, **kwargs: Any) -> dict[str, str]:
    auth = api.app.state.auth_gate.auth  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {auth.create_api_key(name='prog', **kwargs).value}"}


def _get_routes(api: AuthEnv) -> list[str]:
    paths = api.app.openapi()["paths"]  # type: ignore[attr-defined]
    return sorted(path for path, ops in paths.items() if "get" in ops)


def _fill(path: str) -> str:
    out = path
    while "{" in out:
        start, end = out.index("{"), out.index("}")
        out = out[:start] + ULID + out[end + 1 :]
    return out


def test_a_read_key_of_the_admin_is_refused_every_route_a_member_is_refused(api: AuthEnv) -> None:
    member = _member(api)
    read_key = _key(api, read_only=True)
    keyed = api.other_client("10.0.0.30")
    failures: list[tuple[str, object]] = []
    refused_to_member: set[str] = set()
    for route in _get_routes(api):
        path = _fill(route)
        if member.get(path).status_code != 403:
            continue
        refused_to_member.add(route)
        status = keyed.get(path, headers=read_key).status_code
        if route in ADMIN_SURFACES_A_READ_KEY_READS:
            if status == 403:
                failures.append((route, "listed as read by the key, refused"))
        elif status != 403:
            failures.append((route, status))
    assert not failures, failures
    stale = set(ADMIN_SURFACES_A_READ_KEY_READS) - refused_to_member
    assert not stale, f"exceptions that a member is no longer refused: {sorted(stale)}"
    assert set(AUDITED) <= refused_to_member, sorted(set(AUDITED) - refused_to_member)
    assert len(refused_to_member) >= 30  # the walk is not empty: the admin surfaces are many


def test_a_read_key_of_the_admin_still_reads_as_her_profile(api: AuthEnv) -> None:
    read_key = _key(api, read_only=True)
    keyed = api.other_client("10.0.0.34")
    routes = set(_get_routes(api))
    for route in READ_KEY_READS:
        assert route in routes, route
        assert keyed.get(route, headers=read_key).status_code == 200, route


def test_the_admin_session_and_her_admin_key_still_open_her_surfaces(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    admin_key = _key(api)  # scope `admin`, may write
    keyed = api.other_client("10.0.0.31")
    for path in ("/api/v1/stats", "/api/v1/config", "/api/v1/profiles/overview", "/api/v1/plugins"):
        assert api.client.get(path).status_code == 200, path
        assert keyed.get(path, headers=admin_key).status_code == 200, path


def test_the_federated_search_is_refused_to_a_read_key_of_the_admin(api: AuthEnv) -> None:
    keyed = api.other_client("10.0.0.32")
    response = keyed.get(
        "/api/v1/search", params={"q": "tale", "scope": "all"}, headers=_key(api, read_only=True)
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_a_read_key_writes_nothing(api: AuthEnv) -> None:
    keyed = api.other_client("10.0.0.33")
    response = keyed.patch("/api/v1/config", json={}, headers=_key(api, read_only=True))
    assert response.status_code == 403


# -- A3: `--scope admin --read-only` is refused, never an admin key that writes -----------------


def test_an_admin_scope_cannot_be_read_only(api: AuthEnv) -> None:
    engine = create_db_engine(api.settings.database_path)
    try:
        auth = local_auth(engine, api.settings)
        with pytest.raises(ValueError, match="key_scope_read_only_conflict"):
            auth.create_api_key(name="k", scope="admin", read_only=True)
        with pytest.raises(ValueError, match="key_scope_read_only_conflict"):
            auth.create_api_key(
                name="k", scope="request", subject_username="member", read_only=True
            )
        assert auth.create_api_key(name="k", scope="read", read_only=True).prefix
        assert auth.create_api_key(name="k", scope="metrics", read_only=True).prefix
    finally:
        engine.dispose()


def test_the_cli_refuses_an_admin_key_asked_read_only(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    code = run_cli(
        monkeypatch, api.settings, "admin", "keys", "create", "--name", "x", "--scope", "admin",
        "--read-only",
    )  # fmt: skip
    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""  # no secret was printed: no key was made
    assert "--read-only" in err and "admin" in err
    assert not [k for k in api.app.state.auth_gate.auth.api_keys() if k.name == "x"]  # type: ignore[attr-defined]


def test_the_conflict_is_in_the_error_catalogue() -> None:
    from shelfhold.interfaces.http_api.catalog import error_code

    assert error_code("key_scope_read_only_conflict").status == 422


# -- P1: an answer of the API is kept in no cache unless the route says otherwise ---------------


def test_json_answers_of_the_api_are_no_store_by_default(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    for path in ("/api/v1/stats", "/api/v1/profiles/overview", "/api/v1/me", "/api/v1/nope"):
        response = api.client.get(path)
        assert response.headers["Cache-Control"] == "no-store", path


def test_a_route_that_sets_its_own_cache_policy_keeps_it(api: AuthEnv) -> None:
    """A list answered with a content ETag is revalidated by its client: `no-store` would throw
    that away. A page of the server and a file of the build keep their own policy too."""
    assert api.login().status_code == 200
    jobs = api.client.get("/api/v1/jobs")
    assert jobs.status_code == 200 and jobs.headers["ETag"]
    assert "Cache-Control" not in jobs.headers
    again = api.client.get("/api/v1/jobs", headers={"If-None-Match": jobs.headers["ETag"]})
    assert again.status_code == 304 and "Cache-Control" not in again.headers
    assert api.client.get("/pages/login.js").headers["Cache-Control"] == "no-cache"
