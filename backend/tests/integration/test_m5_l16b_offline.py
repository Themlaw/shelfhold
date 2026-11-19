"""M5 L16b (EF-LEC-012 passe 12, EF-LEC-007, EF-USR-006, ENF-SEC-010): the server side of the
offline copies.

A copy kept in a browser is evicted at the reconnection by the removals of `?since=` (M3 L11): the
Work was deleted, purged, or stopped fitting the profile's view. The client keys a copy by the Work
the publication names (`work_id`); it learns nothing else from the server. The real application and
real sessions. The attacks: a child and a Work above her ceiling (never named in her removals, never
a ticket for it), a token carried to another profile, a key of the API, no session at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.application import changes
from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog
from tests.integration.test_l05a_content_route import World, _shape
from tests.integration.test_l05a_content_route import _world as _make_world
from tests.integration.test_m5_l06_publication import ServerCutArchives, book, pages_of, publication

WORKS = "/api/v1/works"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _make_world(monkeypatch, settings)


def token_of(client: Any) -> str:
    answer = client.get(WORKS, params={"limit": 200})
    assert answer.status_code == 200, answer.text
    return str(answer.json()["since"])


def removed(client: Any, token: str) -> Any:
    return client.get(WORKS, params={"since": token, "limit": 200})


def rate(world: World, key: str, age: str) -> None:
    work = world.seed.works[key]
    assert world.env.post(f"{WORKS}/{work}/rating", json={"age": age}).status_code == 200


@pytest.mark.req("EF-LEC-012", "EF-INT-001")
def test_publication_names_the_work_an_offline_copy_is_keyed_by(world: World) -> None:
    content = book(world, pages_of(3))
    got = publication(world.kid, "content", content)
    assert got.status_code == 200, got.text
    assert got.json()["work_id"] == world.seed.works["ok"]
    assert got.headers["cache-control"] == "no-store"


@pytest.mark.req("EF-LEC-012", "EF-INT-001", "EF-USR-006")
def test_offline_copy_evicted_after_reclassification(world: World) -> None:
    """The copy of a Work the admin then rates above the child's ceiling is named in her removals,
    and the Work then answers her publication as a thing that does not exist."""
    content = book(world, pages_of(3))
    work = publication(world.kid, "content", content).json()["work_id"]
    token = token_of(world.kid)
    rate(world, "ok", "18+")
    got = removed(world.kid, token)
    assert got.status_code == 200 and work in got.json()["removed"]
    assert publication(world.kid, "content", content).status_code == 404
    # the member still reads it: her copy is not evicted
    assert work not in removed(world.mum, token_of(world.mum)).json()["removed"]


@pytest.mark.req("EF-LEC-012", "EF-INT-001")
def test_offline_copy_evicted_after_deletion_and_purge(world: World) -> None:
    content = book(world, pages_of(3))
    work = publication(world.kid, "content", content).json()["work_id"]
    token = token_of(world.kid)
    with world.seed.engine.begin() as connection:
        changes.record_work_removed(connection, work)
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == work))
    assert work in removed(world.kid, token).json()["removed"]
    assert publication(world.kid, "content", content).status_code == 404


@pytest.mark.req("EF-LEC-012", "EF-USR-006", "ENF-SEC-010")
def test_since_reveals_nothing_of_a_work_the_child_never_saw(world: World) -> None:
    """A Work above her ceiling is neither in her removals when it is raised again or deleted, nor
    in any answer for its carrier: the same 404 as for a carrier that does not exist."""
    hidden = book(world, pages_of(3), edition="twelve")
    hidden_work = world.seed.works["twelve"]
    token = token_of(world.kid)
    rate(world, "twelve", "18+")
    with world.seed.engine.begin() as connection:
        changes.record_work_removed(connection, hidden_work)
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == hidden_work))
    got = removed(world.kid, token)
    assert got.status_code == 200
    assert hidden_work not in got.text
    absent = publication(world.kid, "content", "0" * 26)
    assert _shape(publication(world.kid, "content", hidden)) == _shape(absent)
    assert "work_id" not in publication(world.kid, "content", hidden).text


@pytest.mark.req("EF-LEC-012", "EF-USR-006", "ENF-SEC-010")
def test_since_token_of_one_profile_is_no_oracle_for_another(world: World) -> None:
    """A token carried to another profile (the scope differs) asks for a resynchronisation and names
    no Work: the client then asks each copy again, which answers 404 for what is hidden."""
    book(world, pages_of(3))
    token = token_of(world.kid)
    rate(world, "ok", "18+")
    carried = removed(world.mum, token)
    assert carried.status_code == 410
    assert world.seed.works["ok"] not in carried.text


@pytest.mark.req("EF-LEC-012", "ENF-SEC-010")
def test_offline_copy_is_not_offered_to_a_key_or_to_nobody(world: World) -> None:
    """A copy is made from a publication, which holds tickets bound to a session: a key of the API
    gets none (403) and a request with no session gets 401."""
    content = book(world, pages_of(3))
    anonymous = world.env.client.__class__(world.env.client.app)
    got = publication(anonymous, "content", content)
    assert got.status_code in {401, 403}
    assert "/content/" not in got.text and "work_id" not in got.text
