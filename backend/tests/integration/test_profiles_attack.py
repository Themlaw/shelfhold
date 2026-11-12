"""M3 L05 attack tests: profile management is a reserved action and a role never escalates.

Every refusal is tried through the real application (`create_app()`, real sessions, real CSRF),
through the service and through the host CLI, and the database is read back to prove that nothing
changed (EF-USR-001, EF-USR-005, EF-USR-022, ENF-SEC-008, ENF-SEC-010).
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Credential,
    LocalAuth,
    Principal,
    ProfileError,
)
from tests.auth_env import ADMIN, CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows, run_cli

PROFILES = "/api/v1/profiles"
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
FAMILY = "gutenberg"
OTHER = "suwayomi"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _auth(env: AuthEnv) -> LocalAuth:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]


def _sql(env: AuthEnv, sql: str, *params: object) -> None:
    connection = sqlite3.connect(env.settings.database_path)
    try:
        with connection:
            connection.execute(sql, params)
    finally:
        connection.close()


def _row(env: AuthEnv, username: str) -> tuple[object, ...]:
    rows = db_rows(
        env.settings,
        'SELECT role, can_rate, max_age_rating FROM "user" WHERE username = ?',
        username,
    )
    return rows[0] if rows else ()


def _users(env: AuthEnv) -> list[tuple[object, ...]]:
    return db_rows(
        env.settings, 'SELECT username, role, can_rate, max_age_rating FROM "user" ORDER BY id'
    )


def _id(env: AuthEnv, username: str) -> str:
    return str(db_rows(env.settings, 'SELECT id FROM "user" WHERE username = ?', username)[0][0])


def _add(env: AuthEnv, username: str, role: str, **kwargs: Any) -> str:
    return _auth(env).profile_add(username, PASSWORD, role, **kwargs)


def _send(env: AuthEnv, method: str, path: str, body: Any = None) -> Any:
    headers = {CSRF_HEADER: env.csrf()}
    if body is None:
        return env.client.request(method, path, headers=headers)
    return env.client.request(method, path, headers=headers, json=body)


def _as(env: AuthEnv, username: str) -> None:
    env.client.cookies.clear()
    assert env.login(username).status_code == 200


def _family_source(env: AuthEnv, key: str, *, family: bool) -> None:
    _sql(
        env,
        "INSERT OR REPLACE INTO source_config (plugin_id, family_safe, settings) "
        "VALUES (?, ?, '{}')",
        key,
        int(family),
    )


def _audit(env: AuthEnv, action: str) -> list[tuple[object, ...]]:
    return db_rows(env.settings, "SELECT session_id, auth_time FROM audit WHERE action = ?", action)


# -- management is reserved: fresh session, admin, never a key or a token --------------------------


@pytest.mark.req("EF-USR-022", "EF-USR-024", "ENF-SEC-010")
def test_profile_management_is_reserved_and_fresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    kid = _add(api, "kid", "child", max_age_rating=10)
    _add(api, "mum", "member")
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "k") == 0
    key = KEY.search(capsys.readouterr().out)
    assert key
    calls = (
        ("POST", PROFILES, {"username": "x", "password": PASSWORD, "role": "member"}),
        ("PATCH", f"{PROFILES}/{kid}", {"max_age_rating": 12}),
        ("DELETE", f"{PROFILES}/{kid}", None),
        ("PUT", f"{PROFILES}/{kid}/source-policies/{FAMILY}", {"policy": "request"}),
    )
    before = _users(api)
    # a key, whatever its scope, and the other roles: refused, nothing written
    for method, path, body in calls:
        keyed = api.client.request(
            method, path, headers={"Authorization": f"Bearer {key.group(0)}"}, json=body
        )
        assert keyed.status_code == 403, path
        assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
    for username in ("kid", "mum"):
        _as(api, username)
        for method, path, body in calls:
            assert _send(api, method, path, body).status_code == 403, (username, path)
    assert _users(api) == before
    # the admin: stale session is asked to prove again; the audit line shows who and when
    _as(api, ADMIN)
    api.clock.advance(timedelta(minutes=20))
    for method, path, body in calls:
        stale = _send(api, method, path, body)
        assert stale.status_code == 401, path
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert _users(api) == before
    _as(api, ADMIN)  # fresh again: allowed, and audited with the session and its auth_time
    created = _send(api, "POST", PROFILES, calls[0][2])
    assert created.status_code == 201, created.text
    assert _row(api, "x") == ("member", 0, None)
    (line,) = _audit(api, "profile.create")
    assert line[0] and line[1]
    assert RESERVED_ACTIONS["profile.create"].effects  # declared, not ad hoc


@pytest.mark.req("EF-USR-022", "ENF-SEC-008")
def test_profile_management_is_fresh_in_every_profile_even_with_nobody_to_protect(
    api: AuthEnv,
) -> None:
    # no child exists yet: the parental rule (fresh only once a restricted profile exists) does
    # not apply, and the management still asks for a fresh proof (EF-USR-022)
    assert _row(api, "kid") == ()
    api.login()
    api.clock.advance(timedelta(minutes=20))
    stale = _send(api, "POST", PROFILES, {"username": "k", "password": PASSWORD, "role": "member"})
    assert stale.status_code == 401
    assert _row(api, "k") == ()


# -- escalation of role -----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-005", "ENF-SEC-008")
def test_member_cannot_promote_self_or_another_by_the_api(api: AuthEnv) -> None:
    _add(api, "mum", "member")
    dad = _add(api, "dad", "member")
    mum = _id(api, "mum")
    before = _users(api)
    _as(api, "mum")
    for target in (mum, dad):
        refused = _send(api, "PATCH", f"{PROFILES}/{target}", {"role": "admin"})
        assert refused.status_code == 403
        assert refused.json()["type"] == "/api/v1/problems/forbidden"
    assert _send(api, "PATCH", f"{PROFILES}/{mum}", {"can_rate": True}).status_code == 403
    created = _send(
        api, "POST", PROFILES, {"username": "boss", "password": PASSWORD, "role": "admin"}
    )
    assert created.status_code == 403
    assert _users(api) == before  # nobody became an admin, nobody was created


@pytest.mark.req("EF-USR-005", "ENF-SEC-008")
def test_member_cannot_promote_by_the_service_or_the_cli_channel(api: AuthEnv) -> None:
    mum = _add(api, "mum", "member")
    dad = _add(api, "dad", "member")
    service = _auth(api)
    before = _users(api)
    attackers = [
        # a member session, a member that a forged `local_cli` credential would carry, a key
        Principal(user_id=mum, username="mum", role="member", credential=Credential.SESSION,
                  session_id="s", auth_time=api.clock.now()),
        Principal(user_id=mum, username="mum", role="member", credential=Credential.LOCAL_CLI),
        Principal(user_id=mum, username="mum", role="admin", credential=Credential.API_KEY,
                  key_id="k", scopes=("admin",)),
        Principal(user_id=mum, username="mum", role="admin", credential=Credential.DEVICE_TOKEN,
                  key_id="d"),
    ]  # fmt: skip
    for attacker in attackers:
        for target in (mum, dad):
            with pytest.raises(ProfileError) as refused:
                service.profile_set(target, role="admin", by=attacker)
            assert refused.value.code == "not_allowed"
        with pytest.raises(ProfileError):
            service.profile_add("boss", PASSWORD, "admin", by=attacker)
        with pytest.raises(ProfileError):
            service.profile_remove(dad, by=attacker)
        with pytest.raises(ProfileError):
            service.profile_policy_set(dad, FAMILY, "add_directly", by=attacker)
    assert _users(api) == before


@pytest.mark.req("EF-USR-005", "ENF-SEC-008")
def test_only_a_fresh_admin_or_the_host_makes_an_admin_and_there_are_at_most_two(
    monkeypatch: pytest.MonkeyPatch, api: AuthEnv
) -> None:
    mum = _add(api, "mum", "member", can_rate=True)
    api.login()
    promoted = _send(api, "PATCH", f"{PROFILES}/{mum}", {"role": "admin"})
    assert promoted.status_code == 200, promoted.text
    assert _row(api, "mum") == ("admin", 1, None)
    _add(api, "dad", "member")
    third = _send(api, "PATCH", f"{PROFILES}/{_id(api, 'dad')}", {"role": "admin"})
    assert third.status_code == 409  # the second admin is optional, a third is not allowed
    assert third.json()["reason"] == "too_many_admins"
    assert run_cli(monkeypatch, api.settings, "profile", "set", "dad", "--role", "admin") == 1
    assert _row(api, "dad") == ("member", 0, None)


@pytest.mark.req("EF-USR-005", "ENF-SEC-008")
def test_an_admin_cannot_change_own_role_nor_remove_self(api: AuthEnv) -> None:
    mum = _add(api, "mum", "member")
    api.login()
    me = _id(api, ADMIN)
    assert _send(api, "PATCH", f"{PROFILES}/{mum}", {"role": "admin"}).status_code == 200
    # two admins now: neither a demotion of self nor a removal of self is an option
    own = _send(api, "PATCH", f"{PROFILES}/{me}", {"role": "member"})
    assert own.status_code == 409 and own.json()["reason"] == "cannot_change_own_role"
    gone = _send(api, "DELETE", f"{PROFILES}/{me}")
    assert gone.status_code == 409 and gone.json()["reason"] == "cannot_remove_self"
    assert _row(api, ADMIN)[0] == "admin"


@pytest.mark.req("EF-USR-005", "ENF-SEC-008")
def test_the_last_admin_cannot_be_demoted_or_removed(
    monkeypatch: pytest.MonkeyPatch, api: AuthEnv
) -> None:
    service = _auth(api)
    me = _id(api, ADMIN)
    for call in (
        lambda: service.profile_set(me, role="member", by=None),
        lambda: service.profile_set(me, role="child", max_age_rating=3, by=None),
        lambda: service.profile_remove(me, by=None),
    ):
        with pytest.raises(ProfileError) as refused:
            call()
        assert refused.value.code == "last_admin"
    assert run_cli(monkeypatch, api.settings, "profile", "set", ADMIN, "--role", "member") == 1
    assert run_cli(monkeypatch, api.settings, "profile", "remove", ADMIN) == 1
    assert _row(api, ADMIN) == ("admin", 1, None)
    # a second admin demoted by the first leaves the first as the last one
    _add(api, "mum", "member")
    api.login()
    mum = _id(api, "mum")
    assert _send(api, "PATCH", f"{PROFILES}/{mum}", {"role": "admin"}).status_code == 200
    assert _send(api, "PATCH", f"{PROFILES}/{mum}", {"role": "member"}).status_code == 200
    assert [u for u in _users(api) if u[1] == "admin"] == [(ADMIN, "admin", 1, None)]


# -- the parental ceiling and the right to classify -------------------------------------------------


@pytest.mark.req("EF-USR-005", "EF-USR-015")
def test_child_creation_needs_a_ceiling_and_a_member_cannot_create_one(api: AuthEnv) -> None:
    _add(api, "mum", "member")
    body = {"username": "kid", "password": PASSWORD, "role": "child"}
    api.login()
    for ceiling in (None, -1, 100, "twelve"):
        refused = _send(api, "POST", PROFILES, {**body, "max_age_rating": ceiling})
        assert refused.status_code == 422, ceiling
    assert _send(api, "POST", PROFILES, body).status_code == 422  # no ceiling at all
    assert _row(api, "kid") == ()
    created = _send(api, "POST", PROFILES, {**body, "max_age_rating": 10})
    assert created.status_code == 201
    assert _row(api, "kid") == ("child", 0, 10)
    # a member creates nobody: without a ceiling, or above the one it would have
    _as(api, "mum")
    for payload in (body, {**body, "username": "kid2", "max_age_rating": 99}):
        assert _send(api, "POST", PROFILES, payload).status_code == 403
    assert _row(api, "kid2") == ()


@pytest.mark.req("EF-USR-005", "EF-USR-015")
def test_an_adult_cannot_carry_a_ceiling_and_a_demotion_to_child_needs_one(api: AuthEnv) -> None:
    mum = _add(api, "mum", "member", can_rate=True)
    api.login()
    odd = _send(api, "POST", PROFILES, {"username": "p", "password": PASSWORD, "role": "member",
                                        "max_age_rating": 12})  # fmt: skip
    assert odd.status_code == 422
    assert _send(api, "PATCH", f"{PROFILES}/{mum}", {"role": "child"}).status_code == 422
    assert _row(api, "mum") == ("member", 1, None)
    done = _send(api, "PATCH", f"{PROFILES}/{mum}", {"role": "child", "max_age_rating": 8})
    assert done.status_code == 200
    assert _row(api, "mum") == ("child", 0, 8)  # becoming a child takes the right to classify away


@pytest.mark.req("EF-USR-005", "EF-BIB-036")
def test_child_never_gets_rate_right_through_any_door(
    monkeypatch: pytest.MonkeyPatch, api: AuthEnv
) -> None:
    kid = _add(api, "kid", "child", max_age_rating=10)
    api.login()
    body = {"username": "k2", "password": PASSWORD, "role": "child", "max_age_rating": 10}
    assert _send(api, "POST", PROFILES, {**body, "can_rate": True}).status_code == 422
    assert _send(api, "PATCH", f"{PROFILES}/{kid}", {"can_rate": True}).status_code == 422
    service = _auth(api)
    with pytest.raises(ProfileError) as refused:
        service.profile_add("k3", PASSWORD, "child", max_age_rating=10, can_rate=True, by=None)
    assert refused.value.code == "child_cannot_rate"
    assert (
        run_cli(monkeypatch, api.settings, "profile", "set", "kid", "--can-rate") == 1
    )  # the host CLI neither
    assert _row(api, "kid") == ("child", 0, 10)
    # a member may hold it, and it goes away with the role
    mum = _add(api, "mum", "member")
    assert _send(api, "PATCH", f"{PROFILES}/{mum}", {"can_rate": True}).status_code == 200
    assert _row(api, "mum") == ("member", 1, None)


@pytest.mark.req("EF-USR-005", "EF-USR-015", "ENF-SEC-010")
def test_child_cannot_change_own_ceiling_or_policy(api: AuthEnv) -> None:
    kid = _add(api, "kid", "child", max_age_rating=7)
    _family_source(api, FAMILY, family=True)
    before = _row(api, "kid")
    _as(api, "kid")
    assert _send(api, "PATCH", f"{PROFILES}/{kid}", {"max_age_rating": 99}).status_code == 403
    assert _send(api, "PATCH", f"{PROFILES}/{kid}", {"role": "member"}).status_code == 403
    put = _send(
        api, "PUT", f"{PROFILES}/{kid}/source-policies/{FAMILY}", {"policy": "add_directly"}
    )
    assert put.status_code == 403
    assert _send(api, "DELETE", f"{PROFILES}/{kid}").status_code == 403
    assert _row(api, "kid") == before
    assert db_rows(api.settings, "SELECT * FROM profile_source_policy") == []


# -- the per-source policy --------------------------------------------------------------------------


@pytest.mark.req("EF-USR-005", "EF-USR-014")
def test_add_directly_never_given_to_a_child_on_a_source_that_is_not_family(
    api: AuthEnv,
) -> None:
    kid = _add(api, "kid", "child", max_age_rating=10)
    mum = _add(api, "mum", "member")
    _family_source(api, FAMILY, family=True)
    _family_source(api, OTHER, family=False)
    api.login()
    for policy in ("add_directly", "request"):
        refused = _send(api, "PUT", f"{PROFILES}/{kid}/source-policies/{OTHER}", {"policy": policy})
        assert refused.status_code == 422, policy
        assert refused.json()["reason"] == "child_source_not_family"
    assert _auth(api).effective_source_policy(kid, OTHER) == "forbidden"
    assert _auth(api).effective_source_policy(kid, "unknown-source") == "forbidden"
    assert _send(api, "PUT", f"{PROFILES}/{kid}/source-policies/{OTHER}",
                 {"policy": "forbidden"}).status_code == 200  # fmt: skip
    # on a family source it is the admin's call, and the default is "ask" (D14)
    assert _auth(api).effective_source_policy(kid, FAMILY) == "request"
    ok = _send(api, "PUT", f"{PROFILES}/{kid}/source-policies/{FAMILY}", {"policy": "add_directly"})
    assert ok.status_code == 200
    assert _auth(api).effective_source_policy(kid, FAMILY) == "add_directly"
    # the admin takes the source off the family list: the row stays, the door closes
    _family_source(api, FAMILY, family=False)
    assert _auth(api).effective_source_policy(kid, FAMILY) == "forbidden"
    # an adult is free on any source, and an invalid policy is refused for everyone
    assert _send(api, "PUT", f"{PROFILES}/{mum}/source-policies/{OTHER}",
                 {"policy": "request"}).status_code == 200  # fmt: skip
    assert _send(api, "PUT", f"{PROFILES}/{mum}/source-policies/{OTHER}",
                 {"policy": "always"}).status_code == 422  # fmt: skip
    listed = _send(api, "GET", f"{PROFILES}/{kid}/source-policies")
    assert listed.status_code == 200
    assert {(i["source_key"], i["policy"]) for i in listed.json()["items"]} == {
        (OTHER, "forbidden"),
        (FAMILY, "add_directly"),
    }


# -- IDOR and visibility ----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-005", "ENF-SEC-010")
def test_idor_a_member_cannot_touch_another_profile_by_changing_the_id(api: AuthEnv) -> None:
    kid = _add(api, "kid", "child", max_age_rating=7)
    _add(api, "mum", "member")
    dad = _add(api, "dad", "member")
    before = _users(api)
    _as(api, "mum")
    unknown = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
    for target in (
        kid,
        dad,
        _id(api, ADMIN),
        unknown,
        "x",
    ):  # an unknown id answers like a real one
        assert (
            _send(api, "PATCH", f"{PROFILES}/{target}", {"max_age_rating": 99}).status_code == 403
        )
        assert _send(api, "PATCH", f"{PROFILES}/{target}", {"role": "admin"}).status_code == 403
        assert _send(api, "DELETE", f"{PROFILES}/{target}").status_code == 403
        put = _send(api, "PUT", f"{PROFILES}/{target}/source-policies/{FAMILY}",
                    {"policy": "add_directly"})  # fmt: skip
        assert put.status_code == 403
        assert _send(api, "GET", f"{PROFILES}/{target}/source-policies").status_code == 403
    assert _send(api, "GET", PROFILES).status_code == 403  # the list is the admin's
    assert _users(api) == before
    _as(api, ADMIN)  # for the admin an unknown id is a plain 404
    assert _send(api, "PATCH", f"{PROFILES}/{unknown}", {"max_age_rating": 1}).status_code == 404


@pytest.mark.req("EF-USR-001", "ENF-SEC-008")
def test_the_admin_lists_the_profiles_without_any_secret(api: AuthEnv) -> None:
    _add(api, "kid", "child", max_age_rating=7)
    api.login()
    listed = _send(api, "GET", PROFILES)
    assert listed.status_code == 200
    items = {i["username"]: i for i in listed.json()["items"]}
    assert set(items) == {ADMIN, "kid"}
    assert items["kid"]["max_age_rating"] == 7 and items["kid"]["role"] == "child"
    assert "argon2" not in listed.text and "password" not in listed.text


@pytest.mark.req("EF-USR-001")
def test_a_profile_signs_in_without_an_email_and_a_username_is_unique(api: AuthEnv) -> None:
    api.login()
    body = {"username": "Mum", "password": PASSWORD, "role": "member"}
    assert _send(api, "POST", PROFILES, body).status_code == 201
    again = _send(api, "POST", PROFILES, {**body, "username": "mum"})  # NOCASE
    assert again.status_code == 409 and again.json()["reason"] == "username_taken"
    short = _send(api, "POST", PROFILES, {**body, "username": "x", "password": "short"})
    assert short.status_code == 422
    _as(api, "Mum")  # signs in with the username and the password only


@pytest.mark.req("EF-USR-005", "ENF-SEC-008")
def test_a_demoted_profile_loses_its_sessions_and_a_removed_one_its_traces_pseudonymised(
    api: AuthEnv,
) -> None:
    _add(api, "mum", "member")
    mum = _id(api, "mum")
    session = api.other_client("127.0.0.2")
    assert api.login("mum", client=session).status_code == 200
    api.login()
    assert (
        _send(api, "PATCH", f"{PROFILES}/{mum}", {"role": "child", "max_age_rating": 5}).status_code
        == 200
    )
    revoked = db_rows(
        api.settings, "SELECT COUNT(*) FROM session WHERE user_id = ? AND revoked_at IS NULL", mum
    )
    assert revoked == [(0,)]
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    assert _row(api, "mum") == ()
    assert db_rows(api.settings, "SELECT COUNT(*) FROM audit WHERE target = ?", mum) == [(0,)]
