"""M4 L15: the door that changes the preferred Edition of a Work, over the real application.

The change is the administrator's (`POST /api/v1/editions/{edition_id}/preferred`): a child, a
member, an anonymous caller and a read-only key are refused before any service runs, and nothing
moves. The answer says how many profiles were carried over, never whose, and no profile's reading
can be read through it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.schema import catalog
from tests.auth_env import AuthEnv, auth_env
from tests.integration.test_m4_l15_edition_change import FR_HASH, bilingual, chapters
from tests.reading_env import NEW_HASH, ReadingEnv
from tests.visibility_env import add_profile, seed_catalogue

pytestmark = pytest.mark.req("EF-BIB-026", "EF-USR-006")
MISSING = "01HZZZZZZZZZZZZZZZZZZZZZZM"


class World:
    def __init__(self, env: AuthEnv, reading: ReadingEnv, kid: TestClient, member: TestClient):
        self.env, self.reading, self.kid, self.member = env, reading, kid, member
        self.work, self.en, self.fr = bilingual(reading)
        self.en_units, self.en_files = chapters(reading, "en", 2, hashed=NEW_HASH)
        self.fr_units, _ = chapters(reading, "fr", 2, hashed=FR_HASH)
        self.users = {
            str(r.username): str(r.id)
            for r in reading.engine.connect().execute(sa.text("SELECT id, username FROM user"))
        }
        for name in ("admin", "mum"):
            reading.position(self.users[name], self.en_files[1], percent=0.5)

    def url(self, edition: str) -> str:
        return f"/api/v1/editions/{edition}/preferred"

    def key(self, *, read_only: bool) -> str:
        auth = local_auth(self.reading.engine, self.env.settings)
        name = "ro" if read_only else "rw"
        return auth.create_api_key(name=name, username="admin", read_only=read_only).value

    def state(self) -> tuple[Any, Any]:
        with self.reading.engine.connect() as connection:
            editions = connection.execute(
                sa.select(catalog.edition.c.id, catalog.edition.c.preferred).order_by(
                    catalog.edition.c.id
                )
            ).all()
            positions = connection.execute(sa.text("SELECT id FROM reading_position")).all()
        return editions, positions


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200
        seed = seed_catalogue(env)
        kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        member = add_profile(env, "mum", "member", "10.0.0.3")
        yield World(env, ReadingEnv(seed.engine, seed), kid, member)
        seed.engine.dispose()


def test_the_administrator_changes_the_preferred_edition_and_profiles_are_counted(
    world: World,
) -> None:
    got = world.env.post(world.url(world.fr), json={})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body == {"edition_id": world.fr, "work_id": world.work, "transferred": 2, "pending": 0}
    editions, _ = world.state()
    assert dict(editions)[world.fr] is True and dict(editions)[world.en] is False
    for name in ("admin", "mum"):  # each carried over by her own reading
        assert world.fr_units[1] in world.reading.progress(world.users[name])


def test_a_child_a_member_a_read_only_key_and_nobody_are_refused_and_nothing_moves(
    world: World,
) -> None:
    before = world.state()
    url = world.url(world.fr)
    for who in (world.kid, world.member):
        got = world.env.post(url, client=who, json={})
        assert got.status_code == 403, got.text
        assert world.fr not in got.text and world.work not in got.text
    anonymous = TestClient(world.env.app, base_url="https://testserver")
    assert anonymous.post(url, json={}).status_code in (401, 403)
    keyed = world.env.other_client("10.0.0.9")
    got = keyed.post(url, json={}, headers={"Authorization": f"Bearer {world.key(read_only=True)}"})
    assert got.status_code in (401, 403), got.text
    assert world.state() == before


def test_a_writing_key_of_the_administrator_may_change_it_like_the_session(world: World) -> None:
    keyed = world.env.other_client("10.0.0.9")
    got = keyed.post(
        world.url(world.fr), json={},
        headers={"Authorization": f"Bearer {world.key(read_only=False)}"},
    )  # fmt: skip
    assert got.status_code == 200, got.text


def test_an_unknown_edition_and_a_malformed_one_are_refused_alike(world: World) -> None:
    before = world.state()
    assert world.env.post(world.url(MISSING), json={}).status_code == 404
    assert world.env.post(world.url("not-an-id"), json={}).status_code in (400, 422)
    assert world.state() == before


def test_the_answer_never_carries_a_profile_or_a_position(world: World) -> None:
    got = world.env.post(world.url(world.fr), json={})
    text = got.text
    for secret in (*world.users.values(), "mum", "kid", "percent", "locator"):
        assert secret not in text
