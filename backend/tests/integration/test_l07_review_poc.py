"""Review PoCs of PR #210 (M3 L07): sessions, admin reset-password, recovery kit, attacked.

Each test is red on the branch under review and names the objection it proves (B/M/m in
`docs/decisions/M3.md`, section "L07 : contradicteur de la #210 (Opus)").
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.interfaces.http_api.sessions import ADMIN_SESSIONS_PATH, SESSIONS_PATH
from tests.auth_env import ADMIN, PASSWORD, AuthEnv, auth_env
from tests.integration.test_l07_sessions import (  # noqa: F401 - `api` is the fixture
    BROWSER,
    NEW_PASSWORD,
    PHONE,
    _admin,
    _key,
    _send,
    _user,
    api,
)
from tests.integration.test_maintenance_mode import _Ops, set_mode

OTHERS = f"{SESSIONS_PATH}/revoke-others"


def _auth(env: AuthEnv) -> Any:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined]


# -- B1: the maintenance mode freezes the new revocations ---------------------------------------


@pytest.mark.req("EF-USR-011", "EF-INT-018")
@pytest.mark.parametrize("route", ["own", "others", "admin"])
def test_review_poc_session_revocations_stay_open_while_the_mode_is_on(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, route: str
) -> None:
    """L13 B2: what takes an access away stays open during the mode. The three revocation routes
    of L07 are writes that `MaintenanceGuard` does not list: 503, the stolen session reads on."""
    with auth_env(monkeypatch, settings, ops_services=_Ops()) as env:
        admin = _admin(env)
        thief = env.other_client("10.0.0.66")
        assert (
            thief.post(
                "/api/v1/auth/login",
                json={"username": ADMIN, "password": PASSWORD},
                headers={"User-Agent": PHONE},
            ).status_code
            == 200
        )
        stolen = next(i["id"] for i in admin.get(SESSIONS_PATH).json()["items"] if not i["current"])
        set_mode(env, True)
        if route == "own":
            done = _send(env, admin, "DELETE", f"{SESSIONS_PATH}/{stolen}")
        elif route == "others":
            done = _send(env, admin, "POST", OTHERS)
        else:
            done = _send(env, admin, "DELETE", f"{ADMIN_SESSIONS_PATH}/{stolen}")
        assert done.status_code in (200, 204), (route, done.status_code)
        assert thief.get("/api/v1/me").status_code == 401


# -- M1: a reset leaves the API keys of the account alive ---------------------------------------


@pytest.mark.req("EF-USR-011", "EF-USR-023")
def test_review_poc_reset_password_closes_the_api_keys_of_the_account(api: AuthEnv) -> None:  # noqa: F811
    """The kit and `secours.md` sell `admin reset-password` as the answer to a lost or stolen
    device ("les sessions et les clés d'accès de ce compte sont fermées"). A key held by that
    device still reads after the reset: `reset_credentials` closes sessions and passkeys only."""
    _user(api, "mum", "member", "10.0.0.3")
    key = _key(api, "mum", read_only=True)
    reader = api.other_client("10.0.0.70")
    bearer = {"Authorization": f"Bearer {key}"}
    assert reader.get("/api/v1/activity", headers=bearer).status_code == 200
    _auth(api).reset_password("mum", NEW_PASSWORD)
    assert reader.get("/api/v1/activity", headers=bearer).status_code == 401


# -- M2: a password sign-in that races the reset survives it ------------------------------------


@pytest.mark.req("EF-USR-011")
def test_review_poc_a_sign_in_with_the_old_password_cannot_slip_past_a_reset(
    api: AuthEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`login` checks the hash it read, then inserts the session in another transaction. A reset
    that commits in between (the host reacting to a stolen password) closes every session but
    the one being opened with the old password. The passkey path is guarded for exactly this
    (`insert_session` under `BEGIN IMMEDIATE`); the password path is not."""
    _user(api, "mum", "member", "10.0.0.3")
    auth = _auth(api)
    real_verify = auth._hasher.verify
    raced: list[int] = []

    def verify_then_reset(stored: Any, password: str) -> bool:
        ok = bool(real_verify(stored, password))
        if not raced:
            raced.append(auth.reset_password("mum", NEW_PASSWORD))
        return ok

    monkeypatch.setattr(auth._hasher, "verify", verify_then_reset)
    thief = api.other_client("10.0.0.66")
    signed = thief.post("/api/v1/auth/login", json={"username": "mum", "password": PASSWORD})
    monkeypatch.setattr(auth._hasher, "verify", real_verify)
    assert raced, "the reset did not run"
    assert signed.status_code != 200 or thief.get("/api/v1/me").status_code == 401


# -- M3: the service trusts the route for every profile's sessions ------------------------------


@pytest.mark.req("EF-USR-011", "ENF-SEC-010")
def test_review_poc_the_service_refuses_a_member_every_profiles_sessions(api: AuthEnv) -> None:  # noqa: F811
    """L05 (decision 2): the service refuses a second time. `sessions_list_all` and
    `session_revoke_of_other` take any principal: a member, a child or a key reaching them by a
    future route (CLI, socket, a route with the wrong guard) lists and closes the admin's."""
    _user(api, "mum", "member", "10.0.0.3")
    admin = _admin(api)
    mum_id = next(
        i["user_id"]
        for i in admin.get(ADMIN_SESSIONS_PATH).json()["items"]
        if i["username"] == "mum"
    )
    admin_session = next(i["id"] for i in admin.get(SESSIONS_PATH).json()["items"] if i["current"])
    member = Principal(
        user_id=mum_id,
        username="mum",
        role="member",
        credential=Credential.SESSION,
        session_id="01HZZZZZZZZZZZZZZZZZZZZZZM",
    )
    auth = _auth(api)
    try:
        seen = [v.user_id for v in auth.sessions_list_all(member)]
    except Exception:  # refused: what is asked
        seen = []
    assert set(seen) <= {mum_id}, "a member listed every profile's sessions"
    with contextlib.suppress(Exception):  # refused: what is asked
        auth.session_revoke_of_other(member, admin_session)
    assert admin.get("/api/v1/me").status_code == 200, "a member closed the admin's session"


# -- m1: a session past its idle limit is listed (and counted) as live --------------------------


@pytest.mark.req("EF-USR-011")
def test_review_poc_an_idle_expired_session_is_not_listed_as_live(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """`live_sessions` filters on `absolute_expiry` only; in the hardened profile an admin session
    is dead after 30 min idle, yet the list shows it, and `revoke-others` counts it."""
    with auth_env(monkeypatch, settings, hardened=True) as env:
        laptop = env.other_client("10.0.0.1")
        phone = env.other_client("10.0.0.9")
        for client, agent in ((laptop, BROWSER), (phone, PHONE)):
            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"username": ADMIN, "password": PASSWORD, "remember": False},
                    headers={"User-Agent": agent},
                ).status_code
                == 200
            )
        env.clock.advance(timedelta(minutes=20))
        assert laptop.get(SESSIONS_PATH).status_code == 200  # the laptop is used, the phone not
        env.clock.advance(timedelta(minutes=20))
        items = laptop.get(SESSIONS_PATH).json()["items"]
        assert phone.get("/api/v1/me").status_code == 401  # dead: 40 min idle
        assert len(items) == 1, [i["device"] for i in items]
