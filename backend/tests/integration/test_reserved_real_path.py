"""EF-USR-022 / EF-USR-024 / ENF-SEC-010 through the real application and the real socket handler.

The routes are the real ones of `create_app()` that sit behind a reserved action, found in the
route table and not listed by hand; the rest of the registry is played through the real identity
service (and the socket handler for the host), with a real login, a real key and a clock moved by
hand. The answers, the problem bodies and the audit rows are read back from the database.
"""

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import product
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.identity.public import (
    CREDENTIAL_AXIS,
    RESERVED_ACTIONS,
    TABLE_ROLES,
    Credential,
    Principal,
)
from shelfhold.infrastructure.local_socket import Peer, RpcError
from shelfhold.interfaces.local_cli.dispatch import IdentityDispatcher
from shelfhold.interfaces.local_cli.principal import principal_for_peer
from tests.auth_env import ADMIN, CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows, run_cli
from tests.integration.test_auth_keys_reserved import _reserved_routes

KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
PROBLEM_FIELDS = {
    "type", "title", "status", "code", "action", "detail", "request_id", "instance",
    "auth_age_s", "max_age_s", "reauth_method", "reauth_methods",
}  # fmt: skip
ULID_MEMBER = "01HZZZZZZZZZZZZZZZZZZZZZZM"
ULID_CHILD = "01HZZZZZZZZZZZZZZZZZZZZZZC"
ULIDS = {"member": ULID_MEMBER, "child": ULID_CHILD}

PURGE = "/api/v1/units/u1/purge"
IP = "127.0.0.1"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
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


def make_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: AuthEnv
) -> str:
    capsys.readouterr()
    assert run_cli(monkeypatch, env.settings, "admin", "keys", "create", "--name", "k") == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return match.group(0)


def real_reserved_routes(env: AuthEnv) -> list[tuple[str, str, str]]:
    """(method, concrete path, action) of the routes of the real app behind a reserved action."""
    found = []
    for method, path, action in _reserved_routes(env.app):
        assert action in RESERVED_ACTIONS
        found.append((method, re.sub(r"\{[^}]+\}", "x", path), action))
    return sorted(set(found))


def call(env: AuthEnv, method: str, path: str, **kwargs: Any) -> Any:
    if method == "GET":
        return env.client.get(path, **kwargs)
    if method == "DELETE":
        headers = {CSRF_HEADER: env.csrf(), **kwargs.pop("headers", {})}
        return env.client.delete(path, headers=headers, **kwargs)
    return env.post(path, json={}, **kwargs)  # an empty body: a 422 means the rule let it through


def audit_rows(env: AuthEnv, action: str) -> list[tuple[object, ...]]:
    return db_rows(env.settings, "SELECT session_id, auth_time FROM audit WHERE action = ?", action)


@pytest.mark.req("EF-USR-022", "EF-USR-024", "ENF-SEC-010")
def test_real_reserved_routes_follow_the_table_for_a_fresh_then_a_stale_admin(
    api: AuthEnv,
) -> None:
    routes = real_reserved_routes(api)
    assert len(routes) >= 4
    for method, path, action in routes:
        api.login()
        if action == "restore.acknowledge":  # the host CLI only: a fresh session is refused too
            assert call(api, method, path).status_code == 403
            assert audit_rows(api, action) == []
            continue
        needs = RESERVED_ACTIONS[action].needs_fresh_auth(
            restricted_profile_exists=False, hardened=False
        )
        before = len(audit_rows(api, action))
        fresh = call(api, method, path)
        assert fresh.status_code not in (401, 403), (path, fresh.text)
        written = audit_rows(api, action)[before:]
        assert len(written) == 1
        assert written[0][0] and written[0][1]  # EF-USR-022 (3): on every action, fresh or not
        api.clock.advance(timedelta(minutes=20))  # beyond the 900 s of P3
        stale = call(api, method, path)
        if needs:
            assert stale.status_code == 401, (path, action)
            assert stale.json()["type"] == "/api/v1/problems/reauth-required"
            assert len(audit_rows(api, action)) == before + 1  # the refusal wrote no entry
        else:
            assert stale.status_code not in (401, 403), (path, action)


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_real_reserved_routes_refuse_keys_and_non_admin_roles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    key = make_key(monkeypatch, capsys, api)
    for method, path, action in real_reserved_routes(api):
        keyed = api.client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {key}"},
            json=None if method in {"GET", "DELETE"} else {},
        )
        assert keyed.status_code == 403, (path, action)
        assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert all(audit_rows(api, action) == [] for action in RESERVED_ACTIONS)
    for role in ("member", "child"):
        username = add_user(api, role, can_rate=True)  # the flag set by hand changes nothing
        assert api.login(username).status_code == 200
        for method, path, action in real_reserved_routes(api):
            refused = call(api, method, path)
            assert refused.status_code == 403, (role, path, action)
            body = refused.json()
            assert body["type"] == "/api/v1/problems/forbidden"
            assert "auth_age_s" not in body and "max_age_s" not in body  # nothing to learn
            api.clock.advance(timedelta(hours=2))  # stale or not: same answer


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
def test_reauth_problem_leaks_nothing_beyond_the_two_ages(api: AuthEnv) -> None:
    api.login()
    session_cookie = api.client.cookies["__Host-sid"]
    api.clock.advance(timedelta(minutes=20))
    response = api.post(PURGE, json={"typed": "x"})
    assert response.status_code == 401
    body = response.json()
    assert set(body) <= PROBLEM_FIELDS, set(body) - PROBLEM_FIELDS
    assert session_cookie not in response.text and "01" not in str(body["action"])
    assert (body["auth_age_s"], body["max_age_s"]) == (1200, 900)
    assert isinstance(body["auth_age_s"], int) and isinstance(body["max_age_s"], int)


def _service(env: AuthEnv) -> Any:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined]


@pytest.mark.req("EF-USR-022", "EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("name", list(RESERVED_ACTIONS))
def test_service_audits_session_and_auth_time_on_every_reserved_action(
    api: AuthEnv, name: str
) -> None:
    """The real service, the real database: whatever the action, an authorised session leaves
    its `session_id` and `auth_time` behind (fresh or not), and a refusal leaves nothing."""
    service = _service(api)
    principal = service.login(ADMIN, PASSWORD, ip=IP).principal
    verdict = service.authorize(principal, RESERVED_ACTIONS[name], restricted_profile_exists=False)
    if name == "restore.acknowledge":  # the host CLI only: a session is refused, nothing audited
        assert type(verdict).__name__ == "CredentialNotAllowed"
        assert audit_rows(api, name) == []
        return
    assert type(verdict).__name__ == "Allowed"
    [(session_id, auth_time)] = audit_rows(api, name)
    assert session_id == principal.session_id and auth_time
    api.clock.advance(timedelta(minutes=20))
    stale = service.authorize(principal, RESERVED_ACTIONS[name], restricted_profile_exists=False)
    wants_fresh = RESERVED_ACTIONS[name].needs_fresh_auth(
        restricted_profile_exists=False, hardened=False
    )
    assert type(stale).__name__ == ("ReauthRequired" if wants_fresh else "Allowed")
    assert len(audit_rows(api, name)) == (1 if wants_fresh else 2)


@pytest.mark.req("EF-USR-022", "EF-BIB-036")
def test_rate_fresh_auth_only_in_hardened_profile(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    rate = RESERVED_ACTIONS["rate"]
    with auth_env(monkeypatch, settings) as plain_env:
        plain = _service(plain_env)
        principal = plain.login(ADMIN, PASSWORD, ip=IP).principal
        plain_env.clock.advance(timedelta(minutes=20))
        verdict = plain.authorize(principal, rate, restricted_profile_exists=False)
        assert type(verdict).__name__ == "Allowed"
    with auth_env(monkeypatch, settings, hardened=True, with_admin=False) as strict_env:
        strict = _service(strict_env)
        principal = strict.login(ADMIN, PASSWORD, ip=IP, remember=False).principal
        strict_env.clock.advance(
            timedelta(minutes=20)
        )  # inside the 30 min idle of a hardened admin
        verdict = strict.authorize(principal, rate, restricted_profile_exists=False)
        assert type(verdict).__name__ == "ReauthRequired"
        assert (verdict.auth_age_s, verdict.max_age_s) == (1200, 900)
        # and over HTTP, with the real re-authentication
        strict_env.login(remember=False)
        strict_env.clock.advance(timedelta(minutes=20))
        assert strict_env.post(PURGE, json={"typed": "x"}).status_code == 401
        reauth = strict_env.post(
            "/api/v1/auth/reauth", json={"method": "password", "password": PASSWORD}
        )
        assert reauth.status_code == 200


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_every_declared_action_is_reachable_through_the_socket_handler_for_the_host() -> None:
    """The socket yields `local_cli` only (SO_PEERCRED vetted); the handler holds it to the table
    and refuses any other principal, key or session, whatever it carries."""
    auth = SimpleNamespace(now=lambda: datetime.now(UTC), fresh_auth_s=900, hardened=True)
    dispatcher = IdentityDispatcher(auth)  # type: ignore[arg-type]
    host = principal_for_peer(Peer(pid=1, uid=0, gid=0))
    for name in RESERVED_ACTIONS:  # the strictest profile: the host is held to no freshness
        dispatcher._authorize(host, name)
    for credential in (Credential.API_KEY, Credential.DEVICE_TOKEN):
        stranger = Principal(
            user_id="u", username="u", role="admin", credential=credential, key_id="k",
            scopes=("admin",), can_rate=True,
        )  # fmt: skip
        for name in RESERVED_ACTIONS:
            with pytest.raises(RpcError):
                dispatcher._authorize(stranger, name)


@pytest.mark.req("EF-USR-022", "ENF-SEC-010")
def test_cli_prints_the_generated_table_under_the_instance_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "access-table", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert {(r["action"], r["role"], r["credential"]) for r in rows} == set(
        product(RESERVED_ACTIONS, TABLE_ROLES, CREDENTIAL_AXIS)
    )
    rate = {(r["role"], r["credential"]): r["fresh_auth_s"] for r in rows if r["action"] == "rate"}
    assert set(rate.values()) == {None}  # no freshness for classifying by default
    hardened = settings.model_copy(update={"auth_hardened": True})
    assert run_cli(monkeypatch, hardened, "access-table", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["fresh_auth_s"] for r in rows if r["action"] == "rate"} == {900}
    assert run_cli(monkeypatch, settings, "access-table") == 0
    line = next(
        row
        for row in capsys.readouterr().out.splitlines()
        if row.startswith("purge.now\tadmin\tsession\t")
    )
    assert line.endswith("\tReauthRequired\t900")


def _insert_event(env: AuthEnv, *, visibility: str, actor: str | None, payload: str = "{}") -> int:
    connection = sqlite3.connect(env.settings.database_path)
    try:
        with connection:
            cursor = connection.execute(
                "INSERT INTO event "
                "(type, schema_version, payload, occurred_at, actor_id, visibility) "
                "VALUES ('job.dead_lettered', 1, ?, '2026-11-05 09:00:00', ?, ?)",
                (payload, actor, visibility),
            )
            return int(cursor.lastrowid or 0)
    finally:
        connection.close()


def _insert_work(env: AuthEnv) -> str:
    work_id = "01HZZZZZZZZZZZZZZZZZZZZZZW"
    connection = sqlite3.connect(env.settings.database_path)
    try:
        with connection:
            [(kind,)] = connection.execute("SELECT key FROM work_kind LIMIT 1").fetchall()
            connection.execute(
                "INSERT INTO work (id, kind, title, sort_title) VALUES (?, ?, 'Un titre', 'un')",
                (work_id, kind),
            )
    finally:
        connection.close()
    return work_id


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
def test_activity_route_masks_unmask_is_audited_and_refused_to_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    add_user(api, "member")
    work_id = _insert_work(api)
    event_id = _insert_event(
        api, visibility="private", actor=ULID_MEMBER, payload=f'{{"work_id": "{work_id}"}}'
    )
    api.login()
    listed = api.client.get("/api/v1/activity").json()
    [item] = [i for i in listed if i["event_id"] == event_id]
    assert item["title"] is None and item["title_masked"] is True
    assert "Un titre" not in api.client.get("/api/v1/activity").text
    key = make_key(monkeypatch, capsys, api)
    refused = api.client.post(
        f"/api/v1/activity/{event_id}/unmask", headers={"Authorization": f"Bearer {key}"}
    )
    assert refused.status_code == 403
    assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert db_rows(api.settings, "SELECT 1 FROM audit WHERE action = 'request.unmask'") == []
    shown = api.post(f"/api/v1/activity/{event_id}/unmask")
    assert shown.status_code == 200 and shown.json()["title"] == "Un titre"
    [(target, session_id, auth_time)] = db_rows(  # the guard's own line has no target
        api.settings,
        "SELECT target, session_id, auth_time FROM audit "
        "WHERE action = 'request.unmask' AND target IS NOT NULL",
    )
    assert target == f"event:{event_id}" and session_id and auth_time
    # a key reads the feed as the integration role: a private event is not in it at all
    seen = api.client.get("/api/v1/activity", headers={"Authorization": f"Bearer {key}"}).json()
    assert all(i["event_id"] != event_id for i in seen)


def _normal(response: Any) -> tuple[int, dict[str, Any]]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k not in ("request_id",)}


def _insert_request(env: AuthEnv, *, visibility: str | None, requester: str | None) -> str:
    request_id = "01HZZZZZZZZZZZZZZZZZZZZZZR"
    connection = sqlite3.connect(env.settings.database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO download_request (id, target_kind, range_ref, source_binding_id, "
                "state, requested_at, visibility, requester_user_id) "
                "VALUES (?, 'range', '{}', 'x', 'requested', '2026-11-05 09:00:00', ?, ?)",
                (request_id, visibility, requester),
            )
    finally:
        connection.close()
    return request_id


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
def test_unmask_does_not_tell_whether_an_entry_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    add_user(api, "member")
    work_id = _insert_work(api)
    other = _insert_event(
        api, visibility="private", actor=ULID_MEMBER, payload=f'{{"work_id": "{work_id}"}}'
    )
    ops = _insert_event(api, visibility="ops", actor=None)  # nothing masked for the admin
    ids = [other + 1000, other, ops]  # absent, present and masked, present and not masked
    paths = [f"/api/v1/activity/{i}/unmask" for i in ids]
    key = make_key(monkeypatch, capsys, api)
    by_key = [
        _normal(api.client.post(p, headers={"Authorization": f"Bearer {key}"})) for p in paths
    ]
    assert by_key[0][0] == 403 and by_key[0] == by_key[1] == by_key[2]
    assert api.login("member1").status_code == 200
    by_member = [_normal(api.post(p, json={})) for p in paths]
    assert by_member[0][0] == 403 and by_member[0] == by_member[1] == by_member[2]
    api.client.cookies.clear()
    api.login()
    by_admin = [_normal(api.post(p, json={})) for p in paths]
    assert by_admin[1][0] == 200  # the one that is masked is revealed
    gone = [by_admin[0], by_admin[2]]  # absent, and nothing masked: the same 404
    assert gone[0][0] == 404 and gone[0] == gone[1]


@pytest.mark.req("EF-USR-022", "EF-USR-016")
def test_unmask_reauth_problem_carries_action_and_methods_like_other_reserved_routes(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings, hardened=True) as env:
        env.login(remember=False)
        env.clock.advance(timedelta(minutes=20))
        body = env.post("/api/v1/activity/1/unmask", json={})
        assert body.status_code == 401
        problem = body.json()
        assert problem["type"] == "/api/v1/problems/reauth-required"
        other = env.post(PURGE, json={"typed": "x"}).json()
        assert set(problem) == set(other)  # the same members as any reserved route
        assert (problem["auth_age_s"], problem["max_age_s"]) == (1200, 900)
        assert problem["reauth_methods"] == ["password"] and problem["reauth_method"] == "password"


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
def test_download_requests_go_through_activity_view_and_unmask_is_audited(
    api: AuthEnv,
) -> None:
    add_user(api, "member")
    request_id = _insert_request(api, visibility="private", requester=ULID_MEMBER)
    api.login()
    [item] = [i for i in api.client.get("/api/v1/activity").json() if i["event_id"] == request_id]
    assert item["visibility"] == "private" and item["title_masked"] is True
    # the title of a request is the Work behind it: with none resolvable, nothing is revealed
    assert api.post(f"/api/v1/activity/{request_id}/unmask", json={}).status_code == 404
    api.client.cookies.clear()
    api.login("member1")
    seen = api.client.get("/api/v1/activity").json()
    assert [i["event_id"] for i in seen] == [request_id]  # a member sees their own request
    assert seen[0]["title_masked"] is False


@pytest.mark.req("EF-USR-016")
def test_unowned_request_is_an_operation_for_the_admin_only(api: AuthEnv) -> None:
    add_user(api, "member")
    request_id = _insert_request(api, visibility=None, requester=None)
    api.login("member1")
    assert [
        i for i in api.client.get("/api/v1/activity").json() if i["event_id"] == request_id
    ] == []
    api.client.cookies.clear()
    api.login()
    mine = [i for i in api.client.get("/api/v1/activity").json() if i["event_id"] == request_id]
    assert [i["visibility"] for i in mine] == ["ops"]
