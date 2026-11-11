"""Minor findings of the cross-cutting security review (2026-11-11), over the real application."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import PASSWORD, AuthEnv, auth_env, db_rows

pytestmark = pytest.mark.req("ENF-SEC-005")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _add_member(api: AuthEnv) -> None:
    import sqlite3

    (admin_hash,) = db_rows(api.settings, "SELECT password_hash FROM user WHERE role = 'admin'")[0]
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO user (id, username, password_hash, role, can_rate) "
            "VALUES ('01J0000000000000000000MEMB', 'member', ?, 'member', 0)",
            (admin_hash,),
        )
        connection.commit()
    finally:
        connection.close()


def test_an_unknown_destination_is_a_404_not_a_500(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    response = api.client.get("/api/v1/adoption/01JZZZZZZZZZZZZZZZZZZZZZZZ/migration")
    assert response.status_code == 404, response.text


def test_the_openapi_document_is_the_admins_and_about_lists_no_action(api: AuthEnv) -> None:
    _add_member(api)
    member = api.other_client("10.0.0.10")
    assert api.login("member", PASSWORD, client=member).status_code == 200
    assert member.get("/api/v1/openapi.json").status_code == 403
    about = member.get("/api/v1/about")
    assert about.status_code == 200
    assert set(about.json()) == {"version", "source_url"}
    assert api.login().status_code == 200
    assert api.client.get("/api/v1/openapi.json").status_code == 200


def test_the_admin_scope_is_never_given_to_a_non_admin(api: AuthEnv) -> None:
    _add_member(api)
    engine = create_db_engine(api.settings.database_path)
    try:
        auth = local_auth(engine, api.settings)
        with pytest.raises(ValueError, match="scope_requires_admin"):
            auth.create_api_key(name="k", username="member")
        assert auth.create_api_key(name="k", username="member", read_only=True).prefix
        assert auth.create_api_key(name="k", username="admin").prefix
    finally:
        engine.dispose()
