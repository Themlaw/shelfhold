"""EF-USR-022 / EF-USR-024 / ENF-SEC-010 through the real application and the real socket handler.

A probe route is mounted for every declared reserved action behind the real dependency
(`reserved_action`), in the real `create_app()`: each cell of the table (credential x role x
freshness) is played with a real login, a real key or the socket handler, and the answer, the
problem body and the audit row are read back from the database.
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Depends

from shelfhold.config import Settings
from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.identity.public import NO_REAUTH_EFFECTS, RESERVED_ACTIONS, Credential, Principal
from shelfhold.infrastructure.local_socket import Peer, RpcError
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.local_cli.dispatch import IdentityDispatcher
from shelfhold.interfaces.local_cli.principal import principal_for_peer
from tests.auth_env import PASSWORD, AuthEnv, auth_env, db_rows, run_cli

KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
PROBLEM_FIELDS = {
    "type", "title", "status", "code", "action", "detail", "request_id", "instance",
    "auth_age_s", "max_age_s", "reauth_method", "reauth_methods",
}  # fmt: skip
ULID_MEMBER = "01HZZZZZZZZZZZZZZZZZZZZZZM"
ULID_CHILD = "01HZZZZZZZZZZZZZZZZZZZZZZC"
ULIDS = {"member": ULID_MEMBER, "child": ULID_CHILD}
FRESH_BY_DEFAULT = [
    name for name, action in RESERVED_ACTIONS.items() if action.effects - NO_REAUTH_EFFECTS
]
NO_FRESHNESS = ["rate", "request.unmask", "keys.create", "keys.rotate", "keys.revoke",
                "device_token.manage"]  # fmt: skip


def probe_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/_probe")
    for name, action in RESERVED_ACTIONS.items():

        def make(action_name: str = name) -> None:  # one closure per action
            @router.post(f"/{action_name}")
            def run(
                _p: Principal = Depends(reserved_action(RESERVED_ACTIONS[action_name])),
            ) -> dict:
                return {"ok": action_name}

        make()
    return router


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, routers=[probe_router()]) as env:
        yield env


@pytest.fixture
def hardened_api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, routers=[probe_router()], hardened=True) as env:
        yield env


def add_user(env: AuthEnv, role: str, *, can_rate: bool = False) -> str:
    username = f"{role}1"
    digest = Argon2Hasher().hash(PASSWORD)
    connection = sqlite3.connect(env.settings.database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO user (id, username, role, can_rate, password_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (ULIDS[role], username, role, int(can_rate), digest),
            )
    finally:
        connection.close()
    return username


def probe(env: AuthEnv, name: str, **kwargs: object) -> object:
    return env.post(f"/api/v1/_probe/{name}", **kwargs)


def make_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: AuthEnv
) -> str:
    capsys.readouterr()
    assert run_cli(monkeypatch, env.settings, "admin", "keys", "create", "--name", "k") == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return match.group(0)


@pytest.mark.req("EF-USR-022", "EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("name", list(RESERVED_ACTIONS))
def test_fresh_admin_session_is_allowed_and_audited_with_session_and_auth_time(
    api: AuthEnv, name: str
) -> None:
    assert api.login().status_code == 200
    assert probe(api, name).status_code == 200  # type: ignore[attr-defined]
    rows = db_rows(api.settings, "SELECT session_id, auth_time FROM audit WHERE action = ?", name)
    assert len(rows) == 1
    session_id, auth_time = rows[0]
    assert session_id and auth_time


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
@pytest.mark.parametrize("name", list(RESERVED_ACTIONS))
def test_stale_session_follows_the_freshness_column(api: AuthEnv, name: str) -> None:
    api.login()
    api.clock.advance(timedelta(minutes=20))  # beyond the 900 s of P3
    response = probe(api, name)
    restricted = name in ("shelf.max_age_rating", "shelf.allowed_profiles",
                          "profile.kavita_mapping", "target.recheck_child_exposure")  # fmt: skip
    if name in FRESH_BY_DEFAULT:
        assert response.status_code == 401  # type: ignore[attr-defined]
        assert response.json()["type"] == "/api/v1/problems/reauth-required"  # type: ignore[attr-defined]
        assert db_rows(api.settings, "SELECT 1 FROM audit WHERE action = ?", name) == []
    else:  # no restricted profile exists: parental control is not fresh either
        assert response.status_code == 200, (name, restricted)  # type: ignore[attr-defined]
        [(session_id, auth_time)] = db_rows(
            api.settings, "SELECT session_id, auth_time FROM audit WHERE action = ?", name
        )
        assert session_id and auth_time  # audited with both, fresh or not


@pytest.mark.req("EF-USR-022", "EF-BIB-036")
def test_rate_fresh_auth_only_in_hardened_profile(api: AuthEnv, hardened_api: AuthEnv) -> None:
    api.login()
    api.clock.advance(timedelta(minutes=20))
    ok = probe(api, "rate")
    assert ok.status_code == 200  # type: ignore[attr-defined]
    hardened_api.login()
    hardened_api.clock.advance(timedelta(minutes=20))  # inside the 30 min idle of the admin
    refused = probe(hardened_api, "rate")
    assert refused.status_code == 401  # type: ignore[attr-defined]
    body = refused.json()  # type: ignore[attr-defined]
    assert body["type"] == "/api/v1/problems/reauth-required"
    assert (body["auth_age_s"], body["max_age_s"]) == (1200, 900)
    again = hardened_api.post(
        "/api/v1/auth/reauth", json={"method": "password", "password": PASSWORD}
    )
    assert again.status_code == 200
    assert probe(hardened_api, "rate").status_code == 200  # type: ignore[attr-defined]


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
def test_reauth_problem_leaks_nothing_beyond_the_two_ages(api: AuthEnv) -> None:
    api.login()
    session_cookie = api.client.cookies["__Host-sid"]
    api.clock.advance(timedelta(minutes=20))
    body = probe(api, "purge.now").json()  # type: ignore[attr-defined]
    assert set(body) <= PROBLEM_FIELDS, set(body) - PROBLEM_FIELDS
    text = str(body)
    for secret in (session_cookie, "admin", api.clock.current.year.__str__() + "-"):
        assert secret not in text.replace("/api/v1/problems", ""), secret
    assert isinstance(body["auth_age_s"], int) and isinstance(body["max_age_s"], int)
    assert body["max_age_s"] == 900


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
@pytest.mark.parametrize("role", ["member", "child"])
@pytest.mark.parametrize("name", list(RESERVED_ACTIONS))
def test_member_and_child_sessions_are_refused_everything_without_a_hint_of_freshness(
    api: AuthEnv, role: str, name: str
) -> None:
    username = add_user(api, role, can_rate=True)  # the flag set by hand changes nothing
    assert api.login(username).status_code == 200
    for stale in (False, True):
        if stale:
            api.clock.advance(timedelta(hours=2))
        response = probe(api, name)
        assert response.status_code == 403  # type: ignore[attr-defined]
        body = response.json()  # type: ignore[attr-defined]
        assert body["type"] == "/api/v1/problems/forbidden"
        assert "auth_age_s" not in body and "max_age_s" not in body


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("name", list(RESERVED_ACTIONS))
def test_api_key_admin_is_refused_every_action(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    name: str,
) -> None:
    key = make_key(monkeypatch, capsys, api)
    response = api.client.post(f"/api/v1/_probe/{name}", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 403
    assert response.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert db_rows(api.settings, "SELECT 1 FROM audit WHERE action = ?", name) == []


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_every_declared_action_is_reachable_through_the_socket_handler_for_the_host() -> None:
    """The socket yields `local_cli` only (SO_PEERCRED vetted); the handler holds it to the table
    and refuses any other principal, key or session, whatever it carries."""
    auth = SimpleNamespace(now=lambda: datetime.now(UTC), fresh_auth_s=900, hardened=True)
    dispatcher = IdentityDispatcher(auth)  # type: ignore[arg-type]
    host = principal_for_peer(Peer(pid=1, uid=0, gid=0))
    assert host.credential is Credential.LOCAL_CLI
    for name in RESERVED_ACTIONS:
        dispatcher._authorize(host, name)
    for credential in (Credential.API_KEY, Credential.DEVICE_TOKEN):
        stranger = Principal(
            user_id="u", username="u", role="admin", credential=credential, key_id="k",
            scopes=("admin",), can_rate=True,
        )  # fmt: skip
        for name in RESERVED_ACTIONS:
            with pytest.raises(RpcError):
                dispatcher._authorize(stranger, name)
