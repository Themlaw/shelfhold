"""EF-INT-021, ENF-SEC-010: the notification preferences of a profile, through the real
application. They are the caller's own (there is no `{profile_id}` to name another one), a key is
refused, and a child may set hers like anyone."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from tests.auth_env import CSRF_HEADER, PASSWORD, AuthEnv, db_rows
from tests.federated_env import CHILD_ID, MEMBER_ID, FakeSource, api_key_of, instance

PATH = "/api/v1/me/notifications"
GOOD: dict[str, Any] = {
    "notify_mode": "immediate", "quiet_start": "22:30", "quiet_end": "06:45",
    "timezone": "Europe/Paris",
}  # fmt: skip


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with instance(monkeypatch, settings, FakeSource("alpha", name="Alpha")) as env:
        yield env


def client_of(api: AuthEnv, username: str, ip: str) -> TestClient:
    client = api.other_client(ip)
    assert api.login(username, PASSWORD, client=client).status_code == 200
    return client


def put(api: AuthEnv, client: TestClient, body: Any) -> Any:
    return client.put(PATH, json=body, headers={CSRF_HEADER: api.csrf(client)})


def row(api: AuthEnv, user_id: str) -> tuple[object, ...]:
    return db_rows(
        api.settings,
        "SELECT notify_mode, quiet_start, quiet_end, timezone FROM user WHERE id = ?",
        user_id,
    )[0]


@pytest.mark.req("EF-INT-021")
def test_defaults_are_a_digest_with_the_default_quiet_hours_shown(api: AuthEnv) -> None:
    kid = client_of(api, "kid", "10.0.0.51")
    body = kid.get(PATH).json()
    assert body["notify_mode"] == "digest" and body["timezone"] == "UTC"
    assert body["quiet_start"] is None and body["quiet_end"] is None
    assert body["effective_quiet"] == ["21:00", "07:00"]  # what holds while she chose none


@pytest.mark.req("EF-INT-021")
def test_a_profile_sets_and_reads_back_its_own_preferences(api: AuthEnv) -> None:
    member = client_of(api, "member", "10.0.0.52")
    answer = put(api, member, GOOD)
    assert answer.status_code == 200, answer.text
    assert member.get(PATH).json()["effective_quiet"] == ["22:30", "06:45"]
    assert row(api, MEMBER_ID) == ("immediate", "22:30", "06:45", "Europe/Paris")


@pytest.mark.req("EF-INT-021", "ENF-SEC-010")
def test_a_profile_cannot_set_another_ones_preferences(api: AuthEnv) -> None:
    """IDOR: the route names nobody. A body that tries to name someone is refused whole, and a
    path that tries to is not a route."""
    kid = client_of(api, "kid", "10.0.0.53")
    before = {u: row(api, u) for u in (MEMBER_ID, CHILD_ID)}
    for smuggled in ("user_id", "profile_id", "id", "username"):
        answer = put(api, kid, {**GOOD, smuggled: MEMBER_ID})
        assert answer.status_code == 422, smuggled
    for path in (f"{PATH}/{MEMBER_ID}", f"/api/v1/profiles/{MEMBER_ID}/notifications"):
        assert kid.put(path, json=GOOD, headers={CSRF_HEADER: api.csrf(kid)}).status_code in (
            403, 404, 405,
        )  # fmt: skip
    assert {u: row(api, u) for u in (MEMBER_ID, CHILD_ID)} == before


@pytest.mark.req("EF-INT-021", "ENF-SEC-010")
def test_a_key_may_not_read_or_set_notification_preferences(api: AuthEnv) -> None:
    for username, read_only in (("kid", True), ("member", True), ("admin", False)):
        key = api_key_of(api, username, read_only=read_only)
        bare = api.other_client("10.9.9.8")
        headers = {"Authorization": f"Bearer {key}"}
        assert bare.get(PATH, headers=headers).status_code in (401, 403)
        assert bare.put(PATH, json=GOOD, headers=headers).status_code in (401, 403)
    assert row(api, MEMBER_ID)[0] == "digest"


@pytest.mark.req("EF-INT-021")
def test_anonymous_gets_nothing(api: AuthEnv) -> None:
    bare = api.other_client("10.9.9.7")
    assert bare.get(PATH).status_code == 401
    assert bare.put(PATH, json=GOOD).status_code in (401, 403)


@pytest.mark.req("EF-INT-021")
@pytest.mark.parametrize(
    "bad",
    [
        {"notify_mode": "always"},
        {"notify_mode": "digest", "quiet_start": "22:00"},  # one end only
        {"notify_mode": "digest", "quiet_start": "25:00", "quiet_end": "07:00"},
        {"notify_mode": "digest", "quiet_start": "7pm", "quiet_end": "07:00"},
        {"notify_mode": "digest", "timezone": "Mars/Olympus"},
        {"notify_mode": "digest", "timezone": ""},
        {"notify_mode": "digest", "timezone": "../../etc/passwd"},
        {"notify_mode": "digest", "digest_hour": 3},  # not a knob: the digest is at 8
        {},
    ],
)
def test_invalid_preferences_are_refused_and_change_nothing(
    api: AuthEnv, bad: dict[str, Any]
) -> None:
    member = client_of(api, "member", "10.0.0.54")
    before = row(api, MEMBER_ID)
    answer = put(api, member, bad)
    assert answer.status_code == 422
    assert row(api, MEMBER_ID) == before
    text = answer.text
    assert "passwd" not in text  # what was sent does not come back


@pytest.mark.req("EF-INT-021")
def test_quiet_hours_can_be_cleared_to_return_to_the_default(api: AuthEnv) -> None:
    member = client_of(api, "member", "10.0.0.55")
    assert put(api, member, GOOD).status_code == 200
    cleared = {"notify_mode": "digest", "quiet_start": None, "quiet_end": None, "timezone": "UTC"}
    assert put(api, member, cleared).status_code == 200
    assert member.get(PATH).json()["effective_quiet"] == ["21:00", "07:00"]
