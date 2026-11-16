"""M4 L32b: theme B "Console" and the per-user selector (EF-UX-012). The choice is kept by the
server for the signed-in person only: nobody reads or writes another's, whatever the role."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.application.preferences import DEFAULT_THEME, DELIVERED_THEMES
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows

PREFERENCES = "/api/v1/me/preferences"
MEMBER_ID = "01J0000000000000000000MEMB"
CHILD_ID = "01J0000000000000000000KDDX"
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _add_users(api: AuthEnv) -> None:
    (admin_hash,) = db_rows(api.settings, "SELECT password_hash FROM user WHERE role = 'admin'")[0]
    connection = sqlite3.connect(api.settings.database_path)
    try:
        for user_id, username, role, ceiling in (
            (MEMBER_ID, "membre", "member", None),
            (CHILD_ID, "enfant", "child", 8),
        ):
            connection.execute(
                "INSERT INTO user (id, username, password_hash, role, can_rate, max_age_rating) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (user_id, username, admin_hash, role, ceiling),
            )
        connection.commit()
    finally:
        connection.close()


def _client(api: AuthEnv, username: str, ip: str) -> TestClient:
    client = api.other_client(ip)
    assert api.login(username, PASSWORD, client=client).status_code == 200
    return client


def _patch(api: AuthEnv, client: TestClient, body: object) -> int:
    return client.patch(PREFERENCES, json=body, headers={CSRF_HEADER: api.csrf(client)}).status_code


def _theme_of(api: AuthEnv, username: str) -> object:
    return db_rows(api.settings, "SELECT theme FROM user WHERE username = ?", username)[0][0]


@pytest.mark.req("EF-UX-012")
def test_console_is_delivered_and_a_stays_the_default(api: AuthEnv) -> None:
    assert DEFAULT_THEME == "bibliotheque"
    assert DELIVERED_THEMES == ("bibliotheque", "console")
    assert api.login().status_code == 200
    first = api.client.get(PREFERENCES).json()
    assert (first["theme"], first["chosen_theme"]) == ("bibliotheque", None)
    assert first["available_themes"] == ["bibliotheque", "console"]
    assert _patch(api, api.client, {"theme": "console", "color_scheme": "dark"}) == 200
    again = api.client.get(PREFERENCES).json()
    assert (again["theme"], again["chosen_theme"], again["color_scheme"]) == (
        "console",
        "console",
        "dark",
    )
    assert _theme_of(api, "admin") == "console"
    assert _patch(api, api.client, {"theme": None}) == 200  # back to the instance default
    assert api.client.get(PREFERENCES).json()["theme"] == "bibliotheque"


@pytest.mark.req("EF-UX-012")
def test_choice_follows_the_person_across_devices(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    assert _patch(api, api.client, {"theme": "console"}) == 200
    other = api.other_client("203.0.113.20")
    assert api.login(client=other).status_code == 200
    assert other.get(PREFERENCES).json()["theme"] == "console"


@pytest.mark.req("EF-UX-012")
@pytest.mark.parametrize("who", ["membre", "enfant"])
def test_idor_a_choice_never_reaches_another_person(api: AuthEnv, who: str) -> None:
    _add_users(api)
    assert api.login().status_code == 200
    assert _patch(api, api.client, {"theme": "console"}) == 200
    mine = _client(api, who, "203.0.113.21")
    assert mine.get(PREFERENCES).json()["chosen_theme"] is None  # the admin's choice is not mine
    assert _patch(api, mine, {"theme": "bibliotheque", "color_scheme": "light"}) == 200
    assert _theme_of(api, "admin") == "console"  # and mine did not touch the admin's
    # the route names nobody: a target in the body or the address is refused or unknown
    for body in ({"theme": "console", "user_id": "x"}, {"theme": "console", "username": "admin"}):
        assert _patch(api, mine, body) == 422
    assert mine.get("/api/v1/users/admin/preferences").status_code in (404, 405)
    assert mine.get(f"{PREFERENCES}?user_id=admin").json()["chosen_theme"] == "bibliotheque"
    assert _theme_of(api, "admin") == "console"


@pytest.mark.req("EF-UX-012")
def test_a_child_chooses_their_own_theme_only(api: AuthEnv) -> None:
    _add_users(api)
    kid = _client(api, "enfant", "203.0.113.22")
    assert _patch(api, kid, {"theme": "console"}) == 200
    assert _theme_of(api, "enfant") == "console"
    assert _theme_of(api, "membre") is None
    assert _theme_of(api, "admin") is None


@pytest.mark.req("EF-UX-012")
def test_an_api_key_cannot_choose_a_theme(api: AuthEnv) -> None:
    engine = create_db_engine(api.settings.database_path)
    try:
        key = local_auth(engine, api.settings).create_api_key(name="k", username="admin").value
    finally:
        engine.dispose()
    keyed = TestClient(api.app, base_url="https://testserver")  # type: ignore[arg-type]
    headers = {"Authorization": f"Bearer {key}"}
    assert keyed.patch(PREFERENCES, json={"theme": "console"}, headers=headers).status_code == 403
    assert _theme_of(api, "admin") is None


@pytest.mark.req("EF-UX-012")
def test_a_theme_that_is_no_longer_delivered_falls_back(api: AuthEnv) -> None:
    """A row that holds a theme of the database list but not of the delivered one (C, D)
    shows the default: the screen never gets a name it cannot style."""
    assert api.login().status_code == 200
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute("UPDATE user SET theme = 'galerie' WHERE username = 'admin'")
        connection.commit()
    finally:
        connection.close()
    body = api.client.get(PREFERENCES).json()
    assert body["theme"] == "bibliotheque"
    assert body["chosen_theme"] is None


@pytest.mark.req("EF-UX-012")
def test_delivered_themes_are_the_token_folders_of_the_front() -> None:
    folders = sorted(
        p.parent.name for p in (REPO / "frontend/src/lib/themes").glob("*/tokens.json")
    )
    assert sorted(DELIVERED_THEMES) == folders
