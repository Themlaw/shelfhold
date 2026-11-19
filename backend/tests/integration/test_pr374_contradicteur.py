"""Relecture adverse de #374 (M5 L17a, `/lite`) : les sondes du contradicteur.

Chaque test vise un trou cherché sur la branche : un cookie `/lite` qui survit à la fin de son
profil ou à son passage en enfant, un cookie d'un profil rejoué sous le nom d'un autre, un
formulaire posté depuis une origine opaque, une couverture cachée distinguée d'une absente. Un
trou réel serait un xfail strict ; aucun n'a été trouvé, ces sondes restent comme garde-fous.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog
from tests.integration.test_l48_opds import FakeBundles, World, auth_of, make_world
from tests.integration.test_m5_l17a_lite import CSRF_FIELD, browser, signed_in
from tests.visibility_env import VISIBLE_TITLES

pytestmark = pytest.mark.req("EF-LEC-010", "ENF-SEC-010", "EF-USR-006")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings, FakeBundles()) as made:
        yield made


def _session_cookie(client: Any) -> tuple[str, str]:
    for cookie in client.cookies.jar:
        if "lite" in cookie.name.lower() and "flow" not in cookie.name.lower():
            return cookie.name, cookie.value
    raise AssertionError("no /lite session cookie")


def _profile_id(world: World, username: str) -> str:
    found = [p for p in auth_of(world.env).profile_list() if p.username == username]
    assert found, username
    return found[0].id


def test_contradicteur_lite_cookie_dies_with_its_profile(world: World) -> None:
    client = signed_in(world, world.mum, "10.0.9.1")
    assert client.get("/lite/works").status_code == 200
    auth_of(world.env).profile_remove(_profile_id(world, "mum"))
    after = client.get("/lite/works", follow_redirects=False)
    assert after.status_code == 303 and "sign-in" in after.headers["location"]
    assert VISIBLE_TITLES["ok"] not in after.text


def test_contradicteur_lite_cookie_follows_a_profile_turned_child(world: World) -> None:
    """A member's cookie, then the member becomes a child under 10: nothing above 10 shows."""
    client = signed_in(world, world.mum, "10.0.9.2")
    work = world.seed.works["adult_shelf"]
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == work).values(age_rating=16)
        )
    assert client.get(f"/lite/works/{work}").status_code == 200
    auth_of(world.env).profile_set(_profile_id(world, "mum"), role="child", max_age_rating=10)
    # a role change revokes the profile's credentials: the session ends, or at least filters
    after = client.get(f"/lite/works/{work}", follow_redirects=False)
    assert after.status_code in (303, 404), after.status_code
    title = world.seed.title_of("adult_shelf")
    assert title not in after.text
    assert title not in client.get("/lite/works").text
    assert title not in client.get("/lite/search", params={"q": "Quiet"}).text


def test_contradicteur_lite_cookie_cannot_be_moved_to_another_token(world: World) -> None:
    """The public id is signed: a child's cookie edited to name the member's token is refused,
    and so is the member's binding glued to the child's payload."""
    kid = signed_in(world, world.kid, "10.0.9.3")
    name, value = _session_cookie(kid)
    payload, _, mac = value.partition(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    body["t"] = world.mum.public_id
    edited = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    anonymous = browser(world, "10.0.9.4")
    got = anonymous.get(
        "/lite/works", headers={"Cookie": f"{name}={edited}.{mac}"}, follow_redirects=False
    )
    assert got.status_code == 303 and VISIBLE_TITLES["adult_shelf"] not in got.text


def test_contradicteur_lite_sign_in_refuses_an_opaque_origin(world: World) -> None:
    client = browser(world, "10.0.9.5")
    token = CSRF_FIELD.search(client.get("/lite/sign-in").text)
    assert token
    data = {"identifier": world.mum.public_id, "secret": world.mum.secret, "csrf": token.group(1)}
    for headers in ({"Origin": "null"}, {"Origin": "https://testserver.evil.example"}):
        refused = client.post("/lite/sign-in", data=data, headers=headers, follow_redirects=False)
        assert refused.status_code == 403, headers


def test_contradicteur_lite_hidden_cover_is_an_absent_one(world: World) -> None:
    """The child asks for the cover of a hidden Edition, and for an unknown hash: same bytes."""
    kid = signed_in(world, world.kid, "10.0.9.6")
    hidden = kid.get(f"/lite/covers/{world.covers['above']}/160")
    absent = kid.get(f"/lite/covers/{'0' * 64}/160")
    assert hidden.status_code == absent.status_code == 404
    assert hidden.content == absent.content
