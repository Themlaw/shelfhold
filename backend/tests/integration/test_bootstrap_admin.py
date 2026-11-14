"""M3 ENF-UX-001, EF-UX-001: the first administrator is made in the first-run wizard.

Until now only `shelfhold admin create` made the first account, a command typed on the host. The
wizard makes it, behind a secret the administrator put in the compose (`SHELFHOLD_SETUP_TOKEN`):
without it anyone who reaches a new instance first would take it. The door works only while no
administrator exists, whatever the number of requests at once, and a wrong secret is counted like
a wrong password.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import SecretStr

from shelfhold.config import Settings
from tests.auth_env import PASSWORD, AuthEnv, auth_env, db_dump, db_rows

TOKEN = "synthetic-setup-token-0123456789abcdef"  # noqa: S105 - throwaway test value
STATUS = "/api/v1/setup/status"
ROUTE = "/api/v1/setup/admin"
BODY = {"username": "owner", "password": PASSWORD, "setup_token": TOKEN}


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    """A new instance: no account, the secret of the compose set."""
    with auth_env(
        monkeypatch,
        settings.model_copy(update={"setup_token": SecretStr(TOKEN)}),
        with_admin=False,
    ) as env:
        yield env


def admins(env: AuthEnv) -> list[str]:
    rows = db_rows(env.settings, "SELECT username FROM user WHERE role='admin'")
    return [str(r[0]) for r in rows]


@pytest.mark.req("EF-UX-001", "ENF-UX-001")
def test_status_says_a_new_instance_needs_its_first_account_without_any_session(
    fresh: AuthEnv,
) -> None:
    answer = fresh.client.get(STATUS)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"needs_admin": True, "token_configured": True}


@pytest.mark.req("EF-UX-001", "ENF-UX-001")
def test_the_first_administrator_is_made_and_signed_in(fresh: AuthEnv) -> None:
    made = fresh.client.post(ROUTE, json=BODY)
    assert made.status_code == 201, made.text
    assert made.json()["csrf_token"]
    assert admins(fresh) == ["owner"]
    me = fresh.client.get("/api/v1/me")
    assert me.status_code == 200 and me.json()["role"] == "admin"
    assert fresh.client.get(STATUS).json()["needs_admin"] is False
    audit = [str(r[0]) for r in db_rows(fresh.settings, "SELECT action FROM audit")]
    assert "user.created" in audit


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_a_wrong_secret_makes_nothing(fresh: AuthEnv) -> None:
    answer = fresh.client.post(ROUTE, json={**BODY, "setup_token": TOKEN[:-1] + "X"})
    assert answer.status_code == 403, answer.text
    assert answer.json()["code"] == "setup_token_invalid"
    assert admins(fresh) == []
    assert fresh.client.get("/api/v1/me").status_code == 401  # nothing was opened


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_without_a_secret_in_the_compose_the_door_stays_shut(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings, with_admin=False) as env:
        assert env.client.get(STATUS).json() == {"needs_admin": True, "token_configured": False}
        answer = env.client.post(ROUTE, json={**BODY, "setup_token": ""})
        assert answer.status_code in {409, 422}, answer.text
        answer = env.client.post(ROUTE, json=BODY)
        assert answer.status_code == 409 and answer.json()["code"] == "setup_token_not_configured"
        assert admins(env) == []


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_the_door_is_closed_once_an_administrator_exists_whoever_made_it(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Made by the host command first (`with_admin`): the right secret opens nothing."""
    with auth_env(
        monkeypatch, settings.model_copy(update={"setup_token": SecretStr(TOKEN)})
    ) as env:
        assert env.client.get(STATUS).json()["needs_admin"] is False
        answer = env.client.post(ROUTE, json=BODY)
        assert answer.status_code == 409 and answer.json()["code"] == "setup_closed"
        assert "owner" not in admins(env)


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_the_door_is_closed_after_the_first_use(fresh: AuthEnv) -> None:
    assert fresh.client.post(ROUTE, json=BODY).status_code == 201
    other = fresh.other_client("10.0.0.7")
    again = other.post(ROUTE, json={**BODY, "username": "second"})
    assert again.status_code == 409 and again.json()["code"] == "setup_closed"
    assert admins(fresh) == ["owner"]


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_requests_at_once_make_one_administrator_not_two(fresh: AuthEnv) -> None:
    """The check and the insert are one transaction: a race cannot give the instance two owners."""

    def attempt(n: int) -> int:
        client = fresh.other_client(f"10.1.0.{n + 1}")
        return int(client.post(ROUTE, json={**BODY, "username": f"owner{n}"}).status_code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(attempt, range(8)))
    assert codes.count(201) == 1, codes
    assert all(c == 409 for c in codes if c != 201), codes
    assert len(admins(fresh)) == 1


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_guessing_the_secret_is_throttled_like_guessing_a_password(fresh: AuthEnv) -> None:
    codes = [
        fresh.client.post(ROUTE, json={**BODY, "setup_token": f"guess-{n:040d}"}).status_code
        for n in range(14)
    ]
    assert codes[:10] == [403] * 10, codes
    assert 429 in codes[10:], codes
    # a throttled attempt is not looked at: even the right secret waits
    assert fresh.client.post(ROUTE, json=BODY).status_code == 429
    assert admins(fresh) == []


@pytest.mark.req("EF-UX-001", "ENF-SEC-004")
def test_a_form_from_another_site_cannot_use_the_door(fresh: AuthEnv) -> None:
    answer = fresh.client.post(
        ROUTE,
        content="username=owner&password=x&setup_token=y",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert answer.status_code == 403 and answer.json()["code"] == "csrf_failed"
    cross = fresh.client.post(ROUTE, json=BODY, headers={"sec-fetch-site": "cross-site"})
    assert cross.status_code == 403
    assert admins(fresh) == []


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_a_weak_password_is_refused_and_makes_nothing(fresh: AuthEnv) -> None:
    answer = fresh.client.post(ROUTE, json={**BODY, "password": "short"})
    assert answer.status_code in {400, 422}, answer.text
    assert admins(fresh) == []
    assert fresh.client.post(ROUTE, json=BODY).status_code == 201  # the door was not spent


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_neither_the_secret_nor_the_password_is_kept_or_answered(
    fresh: AuthEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    made: Any = fresh.client.post(ROUTE, json=BODY)
    assert made.status_code == 201
    seen = made.text + fresh.client.get(STATUS).text + capsys.readouterr().out
    dump = db_dump(fresh.settings)
    for secret in (TOKEN, PASSWORD):
        assert secret not in seen
        assert secret not in dump


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_the_session_is_the_one_of_a_sign_in_with_its_device_cookie(fresh: AuthEnv) -> None:
    """The same path as `login`: the browser gets the signed device cookie (so that its owner is
    not locked out by third parties later), and it is a live session of the new account."""
    owner = fresh.other_client("198.51.100.7")
    assert owner.post(ROUTE, json=BODY).status_code == 201
    assert "__Host-dev" in owner.cookies
    assert owner.get("/api/v1/me").json()["username"] == "owner"
    sessions = db_rows(fresh.settings, "SELECT user_id FROM session WHERE revoked_at IS NULL")
    assert len(sessions) == 1


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
@pytest.mark.parametrize("secret", ["a" * 40, "ab" * 20, "012345678" * 4])
def test_a_secret_made_of_few_characters_is_refused_at_start(
    settings: Settings, secret: str
) -> None:
    with pytest.raises(ValueError, match="different characters"):
        Settings.model_validate({**settings.model_dump(), "setup_token": secret})


@pytest.mark.req("EF-UX-001", "ENF-SEC-008")
def test_a_secret_that_is_chosen_not_generated_is_refused_at_start(settings: Settings) -> None:
    """Like the forward-auth secret: generated, long enough, never a word."""
    with pytest.raises(ValueError, match="setup_token"):
        Settings.model_validate({**settings.model_dump(), "setup_token": "short"})
