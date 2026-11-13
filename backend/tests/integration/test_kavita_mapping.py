"""M3 L10: the declarative mapping profile -> Kavita account (EF-USR-017, EF-USR-024).

Naming the Kavita account of a child is a parental-control decision: a reserved action. A fresh
admin session or the host does it; a key (admin scope included), a device token, a member and a
child never do, and the profile of someone else is not reachable by guessing an identifier. The
mapping holds a name, never a token, and no message of an audit names the account.
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS, LocalAuth, ProfileError
from tests.auth_env import ADMIN, CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows, run_cli

PROFILES = "/api/v1/profiles"
AUDIT = "/api/v1/audit/kavita-accounts"
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _auth(env: AuthEnv) -> LocalAuth:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]


def _add(env: AuthEnv, username: str, role: str, **kwargs: Any) -> str:
    return _auth(env).profile_add(username, PASSWORD, role, **kwargs)


def _mapped(env: AuthEnv) -> list[tuple[object, ...]]:
    return db_rows(env.settings, 'SELECT username, kavita_username FROM "user" ORDER BY username')


def _send(env: AuthEnv, method: str, path: str, body: Any = None) -> Any:
    headers = {CSRF_HEADER: env.csrf()}
    if body is None:
        return env.client.request(method, path, headers=headers)
    return env.client.request(method, path, headers=headers, json=body)


def _as(env: AuthEnv, username: str) -> None:
    env.client.cookies.clear()
    assert env.login(username).status_code == 200


def _put(env: AuthEnv, profile_id: str, name: str | None) -> Any:
    return _send(env, "PUT", f"{PROFILES}/{profile_id}/kavita-account", {"kavita_username": name})


def _audit_lines(env: AuthEnv) -> list[tuple[object, ...]]:
    return db_rows(
        env.settings,
        "SELECT action, target FROM audit WHERE action = ? ORDER BY id",
        "profile.kavita_mapping_set",
    )


@pytest.mark.req("EF-USR-017")
def test_admin_maps_a_child_to_a_kavita_account_and_clears_it(api: AuthEnv) -> None:
    kid = _add(api, "kid", "child", max_age_rating=10)
    done = _put(api, kid, "  Lea.K  ")
    assert done.status_code == 200, done.text
    assert done.json() == {"profile_id": kid, "kavita_username": "Lea.K"}  # trimmed, nothing more
    assert _mapped(api) == [(ADMIN, None), ("kid", "Lea.K")]
    cleared = _put(api, kid, None)
    assert cleared.status_code == 200 and cleared.json()["kavita_username"] is None
    assert _mapped(api) == [(ADMIN, None), ("kid", None)]


@pytest.mark.req("EF-USR-017", "EF-USR-024")
def test_kavita_mapping_is_reserved_action(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    kid = _add(api, "kid", "child", max_age_rating=10)
    other = _add(api, "kid2", "child", max_age_rating=7)
    _add(api, "mum", "member")
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "k") == 0
    key = KEY.search(capsys.readouterr().out)
    assert key
    before = _mapped(api)
    path = f"{PROFILES}/{kid}/kavita-account"
    body = {"kavita_username": "lea"}
    # a key (any scope) never maps; the refusal is the same for a real id and an invented one
    for target in (kid, "01HZZZZZZZZZZZZZZZZZZZZZZZ"):
        keyed = api.client.request(
            "PUT", f"{PROFILES}/{target}/kavita-account",
            headers={"Authorization": f"Bearer {key.group(0)}"}, json=body,
        )  # fmt: skip
        assert keyed.status_code == 403
        assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
    # a member, and a child (her own profile included), are refused: 403, nothing written
    for username in ("mum", "kid"):
        _as(api, username)
        for target in (kid, other, "01HZZZZZZZZZZZZZZZZZZZZZZZ"):
            refused = _send(api, "PUT", f"{PROFILES}/{target}/kavita-account", body)
            assert refused.status_code == 403, (username, target)
        assert _send(api, "GET", AUDIT).status_code == 403
    assert _mapped(api) == before
    # the admin with a session older than 900 s is asked to prove again
    _as(api, ADMIN)
    api.clock.advance(timedelta(minutes=20))
    stale = _send(api, "PUT", path, body)
    assert stale.status_code == 401
    assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert _mapped(api) == before
    _as(api, ADMIN)
    assert _send(api, "PUT", path, body).status_code == 200
    assert RESERVED_ACTIONS["profile.kavita_mapping"].effects  # declared, not ad hoc


@pytest.mark.req("EF-USR-017", "EF-USR-024")
def test_mapping_takes_a_name_only_never_a_token_and_only_for_a_child(api: AuthEnv) -> None:
    kid = _add(api, "kid", "child", max_age_rating=10)
    mum = _add(api, "mum", "member")
    path = f"{PROFILES}/{kid}/kavita-account"
    # a body with anything else than the name is refused (no token, no key, no password)
    for extra in ("token", "api_key", "password", "kavita_key"):
        refused = _send(api, "PUT", path, {"kavita_username": "lea", extra: "x"})
        assert refused.status_code == 422, extra
        assert "x" not in refused.text.replace("extra", "")
    for bad in ("", "   ", "a" * 129, "lea\nbob", "lea\x00", 12, ["lea"]):
        assert _send(api, "PUT", path, {"kavita_username": bad}).status_code in (400, 422), bad
    # an adult has no Kavita account to audit by this mapping
    adult = _put(api, mum, "mum.k")
    assert adult.status_code == 422 and adult.json()["reason"] == "not_a_child"
    assert _put(api, "01HZZZZZZZZZZZZZZZZZZZZZZZ", "x").status_code == 404
    assert _mapped(api) == [(ADMIN, None), ("kid", None), ("mum", None)]


@pytest.mark.req("EF-USR-017")
def test_a_kavita_account_belongs_to_one_profile_without_telling_whose(api: AuthEnv) -> None:
    one = _add(api, "kid", "child", max_age_rating=10)
    two = _add(api, "kid2", "child", max_age_rating=7)
    assert _put(api, one, "Lea").status_code == 200
    clash = _put(api, two, "lea")  # Kavita does not tell "Lea" from "lea"
    assert clash.status_code == 409 and clash.json()["reason"] == "kavita_username_taken"
    assert "kid" not in clash.text  # it does not name the profile that holds it
    assert _put(api, one, "Lea").status_code == 200  # the same value again: no conflict
    assert _mapped(api) == [(ADMIN, None), ("kid", "Lea"), ("kid2", None)]


@pytest.mark.req("EF-USR-017", "ENF-OBS-003")
def test_kavita_mapping_change_is_audited_without_the_name(api: AuthEnv) -> None:
    kid = _add(api, "kid", "child", max_age_rating=10)
    _put(api, kid, "lea")
    _put(api, kid, "lea")  # unchanged: no line
    _put(api, kid, "lea2")
    _put(api, kid, None)
    lines = _audit_lines(api)
    assert len(lines) == 3
    assert all("lea" not in str(t) for _, t in lines) and all(kid in str(t) for _, t in lines)
    assert [str(t).rsplit("|", 1)[1] for _, t in lines] == ["none>set", "set>set", "set>none"]


@pytest.mark.req("EF-USR-017")
def test_removing_a_profile_frees_its_kavita_account(api: AuthEnv) -> None:
    one = _add(api, "kid", "child", max_age_rating=10)
    _put(api, one, "lea")
    assert _send(api, "DELETE", f"{PROFILES}/{one}").status_code == 204
    two = _add(api, "kid2", "child", max_age_rating=7)
    assert _put(api, two, "lea").status_code == 200


@pytest.mark.req("EF-USR-017", "EF-USR-024")
def test_cli_map_kavita_through_the_socket_and_stopped(
    monkeypatch: pytest.MonkeyPatch, api: AuthEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(api, "kid", "child", max_age_rating=10)
    _add(api, "mum", "member")
    s = api.settings
    assert run_cli(monkeypatch, s, "profile", "map-kavita", "kid", "--kavita-user", "lea") == 0
    assert _mapped(api)[1] == ("kid", "lea")
    capsys.readouterr()
    assert run_cli(monkeypatch, s, "profile", "map-kavita", "mum", "--kavita-user", "x") == 1
    assert run_cli(monkeypatch, s, "profile", "map-kavita", "nobody", "--kavita-user", "x") == 1
    assert run_cli(monkeypatch, s, "profile", "map-kavita", "kid") == 2  # a name or --clear
    assert (
        run_cli(monkeypatch, s, "profile", "map-kavita", "kid", "--kavita-user", "a", "--clear")
        == 2
    )
    assert run_cli(monkeypatch, s, "profile", "map-kavita", "kid", "--clear") == 0
    assert _mapped(api)[1] == ("kid", None)
    lines = _audit_lines(api)
    assert len(lines) == 2 and all("lea" not in str(t) for _, t in lines)
    assert run_cli(monkeypatch, s, "profile", "list", "--json") == 0
    assert "lea" not in capsys.readouterr().out  # the list shows no account name unasked


@pytest.mark.req("EF-USR-017")
def test_service_refuses_what_the_route_refuses(api: AuthEnv) -> None:
    mum = _add(api, "mum", "member")
    with pytest.raises(ProfileError) as adult:
        _auth(api).profile_set_kavita_username(mum, "x")
    assert adult.value.code == "not_a_child"
    with pytest.raises(ProfileError) as unknown:
        _auth(api).profile_set_kavita_username("01HZZZZZZZZZZZZZZZZZZZZZZZ", "x")
    assert unknown.value.code == "not_found"
    connection = sqlite3.connect(api.settings.database_path)
    try:
        assert connection.execute(
            'SELECT count(*) FROM "user" WHERE kavita_username IS NOT NULL'
        ).fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.req("EF-EXP-036")
def test_audit_route_is_the_admins_and_names_no_account(api: AuthEnv) -> None:
    kid = _add(api, "kid", "child", max_age_rating=10)
    _put(api, kid, "lea")
    done = _send(api, "GET", AUDIT)
    assert done.status_code == 200
    body = done.json()
    assert body["targets"] == [] and "lea" not in done.text
    api.client.cookies.clear()
    assert api.client.get(AUDIT).status_code == 401  # nobody signed in
