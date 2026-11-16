"""M3 L03: the theme and the colour scheme are stored per user, on the server (EF-UX-012), and the
source link is offered to anyone who reaches the instance (ENF-LEG-012, AGPL section 13)."""

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows

PREFERENCES = "/api/v1/me/preferences"
SOURCE = "/api/v1/source"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _add_member(api: AuthEnv, username: str = "membre") -> None:
    (admin_hash,) = db_rows(api.settings, "SELECT password_hash FROM user WHERE role = 'admin'")[0]
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO user (id, username, password_hash, role, can_rate) "
            "VALUES (?, ?, ?, 'member', 0)",
            ("01J0000000000000000000MEMB", username, admin_hash),
        )
        connection.commit()
    finally:
        connection.close()


def _patch(api: AuthEnv, body: object, client: TestClient | None = None) -> object:
    c = client or api.client
    return c.patch(PREFERENCES, json=body, headers={CSRF_HEADER: api.csrf(c)})


def _theme_columns(api: AuthEnv, username: str) -> tuple[object, ...]:
    sql = "SELECT theme, color_scheme FROM user WHERE username = ?"
    return db_rows(api.settings, sql, username)[0]


@pytest.mark.req("EF-UX-012")
def test_user_theme_preference_persisted(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    assert api.client.get(PREFERENCES).json()["color_scheme"] == "auto"
    response = _patch(api, {"theme": "bibliotheque", "color_scheme": "dark"})
    assert response.status_code == 200  # type: ignore[attr-defined]
    assert _theme_columns(api, "admin") == ("bibliotheque", "dark")
    # another device: a fresh client, a fresh session, the same answer
    other = api.other_client("203.0.113.9")
    assert api.login(client=other).status_code == 200
    body = other.get(PREFERENCES).json()
    assert body["theme"] == "bibliotheque"
    assert body["color_scheme"] == "dark"


@pytest.mark.req("EF-UX-012")
def test_preference_is_per_user_not_per_instance(api: AuthEnv) -> None:
    _add_member(api)
    assert api.login().status_code == 200
    assert _patch(api, {"color_scheme": "light"}).status_code == 200  # type: ignore[attr-defined]
    member = api.other_client("203.0.113.10")
    assert api.login("membre", PASSWORD, client=member).status_code == 200
    assert member.get(PREFERENCES).json()["color_scheme"] == "auto"
    assert _theme_columns(api, "membre") == (None, "auto")


@pytest.mark.req("EF-UX-012")
def test_theme_null_falls_back_to_the_instance_default(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    first = api.client.get(PREFERENCES).json()
    assert first["theme"] == "bibliotheque"  # theme A is the default of the instance
    assert first["chosen_theme"] is None
    assert _patch(api, {"theme": "bibliotheque"}).status_code == 200  # type: ignore[attr-defined]
    assert api.client.get(PREFERENCES).json()["chosen_theme"] == "bibliotheque"
    assert _patch(api, {"theme": None}).status_code == 200  # type: ignore[attr-defined]
    assert _theme_columns(api, "admin") == (None, "auto")


@pytest.mark.req("EF-UX-012")
@pytest.mark.parametrize(
    "body",
    [
        {"theme": "console"},  # B, C and D are not delivered in M3
        {"theme": "galerie"},
        {"theme": "calme"},
        {"theme": "nope"},
        {"theme": "<script>alert(1)</script>"},
        {"theme": "bibliotheque'; DROP TABLE user; --"},
        {"color_scheme": "neon"},
        {"color_scheme": None},
        {"color_scheme": 3},
        {"role": "admin"},  # nothing else is writable through this route
        {"can_rate": True},
        {"theme": "bibliotheque", "username": "root"},
        [],
        "dark",
    ],
)
def test_refused_preference_changes_nothing(api: AuthEnv, body: object) -> None:
    assert api.login().status_code == 200
    response = _patch(api, body)
    assert response.status_code == 422  # type: ignore[attr-defined]
    assert _theme_columns(api, "admin") == (None, "auto")
    assert db_rows(api.settings, "SELECT role FROM user WHERE username = 'admin'") == [("admin",)]


@pytest.mark.req("EF-UX-012")
def test_preference_needs_a_session_and_the_csrf_token(api: AuthEnv) -> None:
    anonymous = api.client.patch(PREFERENCES, json={"color_scheme": "dark"})
    assert anonymous.status_code == 401
    assert api.login().status_code == 200
    no_token = api.client.patch(PREFERENCES, json={"color_scheme": "dark"})
    assert no_token.status_code == 403
    assert _theme_columns(api, "admin") == (None, "auto")


@pytest.mark.req("EF-UX-012")
def test_an_api_key_has_no_preferences(api: AuthEnv) -> None:
    engine = create_db_engine(api.settings.database_path)
    try:
        key = local_auth(engine, api.settings).create_api_key(name="kid", username="admin").value
    finally:
        engine.dispose()
    keyed = TestClient(api.app, base_url="https://testserver")  # type: ignore[arg-type]
    headers = {"Authorization": f"Bearer {key}"}
    assert keyed.get(PREFERENCES, headers=headers).status_code == 403
    assert (
        keyed.patch(PREFERENCES, json={"color_scheme": "dark"}, headers=headers).status_code == 403
    )
    assert _theme_columns(api, "admin") == (None, "auto")


@pytest.mark.req("ENF-LEG-012")
def test_source_link_is_offered_without_signing_in(api: AuthEnv) -> None:
    response = api.client.get(SOURCE)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"source_url"}  # nothing else: no version, no instance detail
    assert body["source_url"].startswith("https://")
    assert api.login().status_code == 200
    about = api.client.get("/api/v1/about").json()
    assert about["source_url"] == body["source_url"]


@pytest.mark.req("ENF-LEG-012")
def test_source_link_follows_the_configured_fork(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    fork = "https://example.org/fork/shelfhold"
    with auth_env(monkeypatch, settings.model_copy(update={"source_url": fork})) as env:
        assert env.client.get(SOURCE).json() == {"source_url": fork}


@pytest.mark.req("EF-UX-012", "ENF-SEC-004")
def test_patch_preferences_without_csrf_token_is_refused(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    forged = api.client.patch(PREFERENCES, json={"color_scheme": "dark"})  # cookie, no header
    assert forged.status_code == 403
    assert forged.json()["code"] == "csrf_failed"
    wrong = api.client.patch(
        PREFERENCES, json={"color_scheme": "dark"}, headers={CSRF_HEADER: "not-the-token"}
    )
    assert wrong.status_code == 403
    assert _theme_columns(api, "admin") == (None, "auto")


@pytest.mark.req("ENF-LEG-012")
@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "ftp://example.org/x", "//example.org", '"><script>x</script>',
     "https://example.org/a b", "https://example.org/\"onmouseover=\"x"],
)  # fmt: skip
def test_a_source_address_that_is_not_http_is_refused_at_start(url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        Settings(source_url=url)  # type: ignore[call-arg]
    from shelfhold.interfaces.http_api.app import create_http_app

    with pytest.raises(ValueError, match="http"):
        create_http_app(
            health=None,  # type: ignore[arg-type]
            on_startup=lambda: None,
            version="1.0.0",
            source_url=url,
        )


@pytest.mark.req("ENF-LEG-012")
def test_source_link_names_the_code_that_is_served() -> None:
    from shelfhold.interfaces.http_api.about import default_source_url

    sha = "0123456789abcdef0123456789abcdef01234567"
    repo = "https://github.com/Themlaw/shelfhold/tree"
    assert default_source_url("1.0.0", revision=sha, release=True) == f"{repo}/v1.0.0"
    assert default_source_url("1.0.0", revision=sha, release=False) == f"{repo}/{sha}"
    # a revision that is not a commit hash never reaches the link
    assert default_source_url("1.0.0", revision="unknown", release=False) == f"{repo}/v1.0.0"
    assert default_source_url("1.0.0", revision='x"><script>', release=False) == f"{repo}/v1.0.0"


@pytest.mark.req("ENF-LEG-012")
def test_build_source_url_reads_what_the_image_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    from shelfhold.interfaces.http_api.about import build_source_url

    sha = "fedcba9876543210fedcba9876543210fedcba98"
    monkeypatch.setenv("SHELFHOLD_BUILD_REVISION", sha)
    monkeypatch.setenv("SHELFHOLD_BUILD_RELEASE", "false")
    assert build_source_url("1.0.0").endswith(f"/tree/{sha}")
    monkeypatch.setenv("SHELFHOLD_BUILD_RELEASE", "true")
    assert build_source_url("1.0.0").endswith("/tree/v1.0.0")
