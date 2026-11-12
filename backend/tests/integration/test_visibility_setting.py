"""M3 L09 (EF-USR-016, ENF-OBS-003): the `visibility` setting of a profile (`private` or
`household`), the way to read and change it, and the audit of a change made to someone else.

Default: `private` for an adult, `household` for a child. A child is never hidden from the
parents. Changing another profile's visibility is a profile management action (a reserved action,
admin only, never a key) and leaves an audit line; the line holds the profile and the word, no
title and nothing else, and the log line of the audit never holds the new value.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
import structlog.testing
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from tests.auth_env import CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows

ME = "/api/v1/me/visibility"
PROFILES = "/api/v1/profiles"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200
        yield env


def _auth(env: AuthEnv) -> Any:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined]


def _user(env: AuthEnv, name: str, role: str, ip: str, **kw: Any) -> tuple[TestClient, str]:
    profile_id = _auth(env).profile_add(name, PASSWORD, role, **kw)
    client = env.other_client(ip)
    assert env.login(name, client=client).status_code == 200
    return client, profile_id


def _put(env: AuthEnv, client: TestClient, path: str, body: Any) -> Any:
    return client.put(path, json=body, headers={CSRF_HEADER: env.csrf(client)})


def _stored(env: AuthEnv, profile_id: str) -> object:
    return db_rows(env.settings, 'SELECT visibility FROM "user" WHERE id = ?', profile_id)[0][0]


def _audit(env: AuthEnv, action: str) -> list[tuple[object, ...]]:
    return db_rows(
        env.settings, "SELECT actor_id, target, session_id FROM audit WHERE action = ?", action
    )


@pytest.mark.req("EF-USR-016")
def test_visibility_setting_ui(api: AuthEnv) -> None:
    """The API behind the setting of the UI: defaults, change, refusals."""
    mum, mum_id = _user(api, "mum", "member", "10.0.0.3")
    kid, kid_id = _user(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    assert mum.get(ME).json() == {"visibility": "private"}  # adult default
    assert kid.get(ME).json() == {"visibility": "household"}  # child default: seen by the parents
    assert api.client.get(ME).json() == {"visibility": "private"}
    assert _stored(api, mum_id) is None  # nothing is stored until somebody chooses
    changed = _put(api, mum, ME, {"visibility": "household"})
    assert changed.status_code == 200 and changed.json() == {"visibility": "household"}
    assert mum.get(ME).json() == {"visibility": "household"}
    assert _stored(api, mum_id) == "household"
    assert _put(api, mum, ME, {"visibility": "private"}).json() == {"visibility": "private"}
    for bad in ({"visibility": "public"}, {"visibility": ""}, {"visibility": None}, {}, {"x": 1}):
        assert _put(api, mum, ME, bad).status_code == 422, bad
    assert _put(api, mum, ME, {"visibility": "household", "role": "admin"}).status_code == 422
    # a child asks to hide from the parents: refused with the reason, nothing stored
    refused = _put(api, kid, ME, {"visibility": "private"})
    assert refused.status_code == 422 and refused.json()["reason"] == "child_visibility_household"
    assert _stored(api, kid_id) is None
    assert _put(api, kid, ME, {"visibility": "household"}).status_code == 200
    # the profile list tells what holds for each one
    listed = {p["username"]: p["visibility"] for p in api.client.get(PROFILES).json()["items"]}
    assert listed["mum"] == "private" and listed["kid"] == "household"
    assert listed["admin"] == "private"


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
def test_visibility_setting_is_the_callers_own_and_session_only(api: AuthEnv) -> None:
    _mum, mum_id = _user(api, "mum", "member", "10.0.0.3")
    key = (
        local_auth(_engine(api), api.settings)
        .create_api_key(name="k", username="mum", read_only=True)
        .value
    )
    by_key = api.other_client("10.0.0.9")
    headers = {"Authorization": f"Bearer {key}"}
    assert by_key.get(ME, headers=headers).status_code == 403  # a program has no visibility
    assert by_key.put(ME, json={"visibility": "household"}, headers=headers).status_code == 403
    anonymous = api.other_client("10.0.0.10")
    assert anonymous.get(ME).status_code in (401, 403)
    assert anonymous.put(ME, json={"visibility": "household"}).status_code in (401, 403)
    assert _stored(api, mum_id) is None


def _engine(env: AuthEnv) -> sa.Engine:
    from shelfhold.infrastructure.db import create_db_engine

    return create_db_engine(env.settings.database_path)


@pytest.mark.req("EF-USR-016", "ENF-OBS-003")
def test_visibility_change_audited(api: AuthEnv) -> None:
    """Changing someone else's visibility is audited, with the admin who did it; changing your
    own is yours to do and writes no such line; a refusal writes nothing."""
    mum, mum_id = _user(api, "mum", "member", "10.0.0.3")
    _kid, kid_id = _user(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    path = f"{PROFILES}/{mum_id}/visibility"
    done = _put(api, api.client, path, {"visibility": "household"})
    assert done.status_code == 200 and done.json()["visibility"] == "household"
    assert _stored(api, mum_id) == "household"
    rows = _audit(api, "profile.visibility_set")
    assert len(rows) == 1
    actor, target, session = rows[0]
    admin_id = str(db_rows(api.settings, "SELECT id FROM user WHERE role = 'admin'")[0][0])
    assert actor == admin_id and session is not None  # who did it, and from which session
    assert target == f"{mum_id}|household"  # the profile and the word, nothing else
    # the same value again changes nothing and leaves no second line
    assert _put(api, api.client, path, {"visibility": "household"}).status_code == 200
    assert len(_audit(api, "profile.visibility_set")) == 1
    # a refusal leaves nothing: unknown value, a child asked to hide, an unknown profile
    assert _put(api, api.client, path, {"visibility": "public"}).status_code == 422
    kid_path = f"{PROFILES}/{kid_id}/visibility"
    assert _put(api, api.client, kid_path, {"visibility": "private"}).status_code == 422
    assert _put(api, api.client, f"{PROFILES}/01HZZZZZZZZZZZZZZZZZZZZZZZ/visibility",
                {"visibility": "household"}).status_code == 404  # fmt: skip
    assert _stored(api, kid_id) is None
    # the member changes her own: no audit of "someone else's"
    assert _put(api, mum, ME, {"visibility": "private"}).status_code == 200
    assert len(_audit(api, "profile.visibility_set")) == 1
    # the next change by the admin (back to household, over her own choice) is a second line
    assert _put(api, api.client, path, {"visibility": "household"}).status_code == 200
    assert [r[1] for r in _audit(api, "profile.visibility_set")] == [
        f"{mum_id}|household", f"{mum_id}|household",
    ]  # fmt: skip


@pytest.mark.req("EF-USR-016", "ENF-OBS-003", "ENF-SEC-010")
def test_visibility_of_others_is_an_admin_session_action(api: AuthEnv) -> None:
    mum, mum_id = _user(api, "mum", "member", "10.0.0.3")
    _dad, dad_id = _user(api, "dad", "member", "10.0.0.4")
    kid, kid_id = _user(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    for who in (mum, kid):  # a member, a child: not on another's profile, not on her own via it
        for target in (dad_id, kid_id, mum_id):
            assert _put(api, who, f"{PROFILES}/{target}/visibility",
                        {"visibility": "household"}).status_code == 403  # fmt: skip
    key = (
        local_auth(_engine(api), api.settings)
        .create_api_key(name="adminkey", username="admin", read_only=False)
        .value
    )
    by_key = api.other_client("10.0.0.9").put(
        f"{PROFILES}/{dad_id}/visibility", json={"visibility": "household"},
        headers={"Authorization": f"Bearer {key}"},
    )  # fmt: skip
    assert by_key.status_code in (401, 403)  # a key never manages profiles
    assert _stored(api, dad_id) is None and _stored(api, mum_id) is None
    assert _audit(api, "profile.visibility_set") == []


@pytest.mark.req("ENF-OBS-003", "ENF-LEG-009")
def test_visibility_audit_line_in_the_log_carries_no_value(api: AuthEnv) -> None:
    """The log of the audit holds the word of the action, never the visibility chosen nor the
    profile it was chosen for (the diagnostic bundle keeps the word, the golden file lists it)."""
    _, mum_id = _user(api, "mum", "member", "10.0.0.3")
    with structlog.testing.capture_logs() as captured:
        assert _put(api, api.client, f"{PROFILES}/{mum_id}/visibility",
                    {"visibility": "household"}).status_code == 200  # fmt: skip
    audits = [e for e in captured if e.get("event") == "auth.audit"]
    assert audits and audits[-1]["action"] == "profile.visibility_set"
    assert "household" not in str(captured) and mum_id not in str(
        [e for e in captured if e.get("event") == "auth.audit"]
    )
