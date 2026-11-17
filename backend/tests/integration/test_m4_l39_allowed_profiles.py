"""M4 L39 (D-P3): `shelf.allowed_profiles` is read on every Shelfhold surface.

ADR-0027, amended: a Work is visible to a child only if at least one shelf that carries it is open
to her (`allowed_profiles` empty, or naming her), on top of her ceiling. One function says it
(`catalog.queries.visible_works_clause`, and the Editions with it); the catalogue, the search, the
OPDS feed, the progress sync, the collections and the relations all ask it.

The attacks: another child (IDOR on a Work filed for a sibling), a member (not narrowed), an API
key of the child (reads as the child), a read-only key of the child (the same, never more), and a
profile taken off the list of a shelf (closes at the next request). A hidden identifier answers
exactly like an absent one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.catalog.public import (
    count_visible_works,
    list_visible_works,
    visible_edition_ids,
    visible_unit_ids,
    visible_work_ids,
)
from shelfhold.config import Settings
from shelfhold.identity.public import local_auth, reader_scope, scope_of
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.auth_env import AuthEnv, auth_env, run_cli
from tests.integration.test_schema_catalog import add_unit
from tests.visibility_env import Seed, add_profile, seed_catalogue

pytestmark = pytest.mark.req("EF-USR-006", "EF-USR-015", "ENF-SEC-010")

SEARCH = "/api/v1/search"
ME = "/api/v1/me"
ABSENT = "01HZZZZZZZZZZZZZZZZZZZZZZM"
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


class World:
    """The shared catalogue plus a Work filed only on a shelf of one child (`sib`); `kid` is
    another child, `mum` an adult member."""

    def __init__(
        self, env: AuthEnv, seed: Seed, kid: TestClient, sib: TestClient, mum: TestClient
    ) -> None:
        self.env, self.seed, self.kid, self.sib, self.mum = env, seed, kid, sib, mum
        with seed.engine.connect() as connection:
            self.kid_id = self._id(connection, "kid")
            self.sib_id = self._id(connection, "sib")
        self.shelf_id = ""
        self.work_id = ""
        self.edition_id = ""
        self.unit_id = ""

    @staticmethod
    def _id(connection: sa.Connection, name: str) -> str:
        return str(
            connection.execute(sa.text("SELECT id FROM user WHERE username=:n"), {"n": name})
            .scalar_one()
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        seed = seed_catalogue(env)
        kid = add_profile(env, "kid", "child", "10.0.0.2", max_age_rating=10)
        sib = add_profile(env, "sib", "child", "10.0.0.9", max_age_rating=10)
        mum = add_profile(env, "mum", "member", "10.0.0.3")
        made = World(env, seed, kid, sib, mum)
        with seed.engine.begin() as connection:
            kind = str(connection.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one())
            made.shelf_id = new_ulid()
            connection.execute(
                sa.insert(export.shelf).values(
                    id=made.shelf_id, name="Sib only", phrase="sib only", max_age_rating=10,
                    allowed_profiles=[made.sib_id],
                )
            )  # fmt: skip
            made.work_id = new_ulid()
            connection.execute(
                sa.insert(catalog.work).values(
                    id=made.work_id, kind=kind, title="Tale of Sibling Shelf",
                    sort_title="tale of sibling shelf", age_rating=6, age_x=False,
                    rated_at=NOW, is_adult=False,
                )
            )  # fmt: skip
            made.edition_id = new_ulid()
            connection.execute(
                sa.insert(catalog.edition).values(
                    id=made.edition_id, work_id=made.work_id, lang="en",
                    translation_type="official", media="images", rated_at=NOW,
                    shelf_id=made.shelf_id,
                )
            )  # fmt: skip
            made.unit_id = add_unit(connection, made.edition_id)
        assert run_cli(monkeypatch, env.settings, "search", "rebuild") == 0
        yield made
        seed.engine.dispose()


def _titles(response: Any) -> list[str]:
    return [hit["title"] for hit in response.json()["items"]]


def _shape(response: Any) -> tuple[int, Any]:
    return response.status_code, response.json() if response.content else None


# -- the one function ---------------------------------------------------------------------------


def test_visible_work_ids_reads_the_shelves_of_the_profile(world: World) -> None:
    """The scope carries the profile (`reader_scope`), and the shelves it names are open to it."""
    with world.seed.engine.connect() as connection:
        sib = reader_scope(connection, world.sib_id)
        kid = reader_scope(connection, world.kid_id)
        assert visible_work_ids(connection, sib, [world.work_id]) == {world.work_id}
        assert visible_work_ids(connection, kid, [world.work_id]) == set()
        # no profile at all (a scope built from a role and a ceiling): only the shelves open to all
        assert visible_work_ids(connection, scope_of("child", 10), [world.work_id]) == set()


def test_edition_and_unit_follow_the_shelf_of_the_edition(world: World) -> None:
    with world.seed.engine.connect() as connection:
        sib = reader_scope(connection, world.sib_id)
        kid = reader_scope(connection, world.kid_id)
        assert visible_edition_ids(connection, sib, [world.edition_id]) == {world.edition_id}
        assert visible_edition_ids(connection, kid, [world.edition_id]) == set()
        assert visible_unit_ids(connection, kid, [world.unit_id]) == set()
        assert visible_unit_ids(connection, sib, [world.unit_id]) == {world.unit_id}


def test_an_edition_on_a_reserved_shelf_is_not_hers_even_when_the_work_is(world: World) -> None:
    """A Work on two shelves, one reserved to the sibling: the open one carries it for `kid`, and
    the Edition of the reserved one stays out of her view (no cover, no unit, no progress)."""
    with world.seed.engine.begin() as connection:
        open_edition = new_ulid()
        connection.execute(
            sa.insert(catalog.edition).values(
                id=open_edition, work_id=world.work_id, lang="fr",
                translation_type="official", media="images", rated_at=NOW,
                shelf_id=world.seed.shelves["adult"],
            )
        )  # fmt: skip
    with world.seed.engine.connect() as connection:
        kid = reader_scope(connection, world.kid_id)
        assert visible_work_ids(connection, kid, [world.work_id]) == {world.work_id}
        assert visible_edition_ids(connection, kid, [world.edition_id, open_edition]) == {
            open_edition
        }


def test_an_edition_on_no_shelf_carries_nothing_for_a_child(world: World) -> None:
    """D-P3, literally: a rated Work no shelf carries is not hers (an adult still sees it)."""
    with world.seed.engine.begin() as connection:
        kind = str(connection.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one())
        loose = new_ulid()
        connection.execute(
            sa.insert(catalog.work).values(
                id=loose, kind=kind, title="Tale of Loose Leaf", sort_title="tale of loose leaf",
                age_rating=6, age_x=False, rated_at=NOW, is_adult=False,
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(catalog.edition).values(
                id=new_ulid(), work_id=loose, lang="en", translation_type="official",
                media="images", rated_at=NOW, shelf_id=None,
            )
        )  # fmt: skip
    with world.seed.engine.connect() as connection:
        kid = reader_scope(connection, world.kid_id)
        assert visible_work_ids(connection, kid, [loose]) == set()
        assert visible_work_ids(connection, scope_of("member", None), [loose]) == {loose}


def test_counts_pages_and_series_come_from_the_same_clause(world: World) -> None:
    with world.seed.engine.connect() as connection:
        sib, kid = reader_scope(connection, world.sib_id), reader_scope(connection, world.kid_id)
        assert count_visible_works(connection, sib) == count_visible_works(connection, kid) + 1
        listed = {w.work_id for w in list_visible_works(connection, kid, limit=200, offset=0)}
        assert world.work_id not in listed
        listed = {w.work_id for w in list_visible_works(connection, sib, limit=200, offset=0)}
        assert world.work_id in listed
        on_shelf = list_visible_works(connection, kid, limit=50, offset=0, shelf_id=world.shelf_id)
        assert on_shelf == []  # a filter by shelf is one more AND on the one filter


# -- the surfaces: another child, a member, a key -------------------------------------------------


def test_search_is_narrowed_for_the_other_child_and_not_for_the_named_one(world: World) -> None:
    assert _titles(world.kid.get(SEARCH, params={"q": "Sibling"})) == []
    assert _titles(world.sib.get(SEARCH, params={"q": "Sibling"})) == ["Tale of Sibling Shelf"]
    assert _titles(world.mum.get(SEARCH, params={"q": "Sibling"})) == ["Tale of Sibling Shelf"]
    assert _titles(world.env.client.get(SEARCH, params={"q": "Sibling"})) == [
        "Tale of Sibling Shelf"
    ]  # the member and the admin are not narrowed by a shelf


@pytest.mark.parametrize("read_only", [True, False])
def test_a_key_of_the_child_reads_as_the_child(world: World, read_only: bool) -> None:
    """A key (even a read-only one, even a full one) reads as the profile it was issued for: the
    shelves of that profile apply, never the ones of whoever created the key."""
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey", username="kid", read_only=read_only)
        .value
    )
    answer = world.env.other_client("10.0.0.52").get(
        SEARCH, params={"q": "Sibling"}, headers={"Authorization": f"Bearer {key}"}
    )
    assert answer.status_code == 200 and _titles(answer) == []
    assert "Sibling" not in answer.text and world.work_id not in answer.text


def test_idor_on_the_state_of_a_work_of_a_sibling_shelf(world: World) -> None:
    """A Work filed only on a shelf of another child answers exactly like an absent identifier,
    on the read and on the write; nothing is written for it."""
    for method, body in (("GET", None), ("PUT", {"followed": True})):
        for path in (f"{ME}/state/{{}}", f"{ME}/works/{{}}/title"):
            if method == "PUT" and path.endswith("/title"):
                continue
            absent = _call(world, method, path.format(ABSENT), body)
            hidden = _call(world, method, path.format(world.work_id), body)
            assert hidden.status_code == 404 and _shape(hidden) == _shape(absent), (method, path)
            assert world.work_id not in hidden.text
    with world.seed.engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT work_id FROM user_state")).all()
    assert rows == []
    named = world.sib.get(f"{ME}/state/{world.work_id}")
    assert named.status_code == 200  # the control: the child the shelf names reads it


def _call(world: World, method: str, path: str, body: Any) -> Any:
    from tests.auth_env import CSRF_HEADER

    headers = {CSRF_HEADER: world.env.csrf(world.kid)}
    return world.kid.request(method, path, json=body, headers=headers)


def test_the_list_of_works_and_the_series_hold_no_reserved_work(world: World) -> None:
    listed = world.kid.get("/api/v1/works", params={"limit": 200})
    assert listed.status_code == 200
    assert world.work_id not in listed.text and "Sibling" not in listed.text
    named = world.sib.get("/api/v1/works", params={"limit": 200})
    assert world.work_id in named.text


def test_taking_the_profile_off_the_list_closes_at_the_next_request(world: World) -> None:
    """The list of a shelf is read from the database at each request: no cache of it survives."""
    assert _titles(world.sib.get(SEARCH, params={"q": "Sibling"})) != []
    service_shelf = world.seed.engine
    with service_shelf.begin() as connection:
        connection.execute(
            sa.update(export.shelf)
            .where(export.shelf.c.id == world.shelf_id)
            .values(allowed_profiles=[world.kid_id])
        )
    assert _titles(world.sib.get(SEARCH, params={"q": "Sibling"})) == []
    assert _titles(world.kid.get(SEARCH, params={"q": "Sibling"})) != []


def test_a_shelf_open_to_everyone_hides_nothing(world: World) -> None:
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(export.shelf)
            .where(export.shelf.c.id == world.shelf_id)
            .values(allowed_profiles=[])
        )
    assert _titles(world.kid.get(SEARCH, params={"q": "Sibling"})) == ["Tale of Sibling Shelf"]


def test_the_opds_feed_does_not_carry_the_reserved_work(world: World) -> None:
    token = world.env.app.state.auth_gate.auth.create_device_token(subject_username="kid")  # type: ignore[attr-defined]
    import base64

    headers = {
        "Authorization": "Basic "
        + base64.b64encode(f"{token.public_id}:{token.secret}".encode()).decode()
    }
    client = world.env.other_client("10.0.0.61")
    absent = client.get(f"/opds/works/{ABSENT}", headers=headers)
    hidden = client.get(f"/opds/works/{world.work_id}", headers=headers)
    assert hidden.status_code == 404 and _shape(hidden) == _shape(absent)
    for path in ("/opds/works", f"/opds/shelves/{world.shelf_id}"):
        got = client.get(path, headers=headers, params={"limit": 200})
        assert "Sibling" not in got.text and world.work_id not in got.text, path
