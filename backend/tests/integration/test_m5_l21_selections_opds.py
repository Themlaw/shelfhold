"""M5 L21 (EF-INT-007, EF-BIB-017, EF-USR-006): a saved selection is an OPDS entry.

The feed of a selection is made of what the profile of the device token may see, read at each
request: a child never gets a hidden Work from it, nor in its total, nor on a page boundary, and a
selection that is not the profile's is one that does not exist. Real application, real tokens.
"""

# ruff: noqa: F811  (the fixture `world` is that of the OPDS suite)
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog
from tests.auth_env import CSRF_HEADER
from tests.integration.test_l48_opds import (  # noqa: F401  (fixtures and readers)
    SEEN_KID,
    World,
    crawl,
    entries,
    link,
    links,
    parse,
    titles,
    total,
    work_titles,
    world,
)
from tests.visibility_env import HIDDEN_TITLES, VISIBLE_TITLES

pytestmark = pytest.mark.req("EF-INT-007", "EF-BIB-017")

SELECTIONS = "/api/v1/selections"
ABSENT = "01HZZZZZZZZZZZZZZZZZZZZZZM"


def _make(world: World, client: Any, name: str, query: dict[str, Any]) -> str:
    made = client.post(
        SELECTIONS, json={"name": name, "query": query},
        headers={CSRF_HEADER: world.env.csrf(client)},
    )  # fmt: skip
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _kind(world: World) -> str:
    with world.seed.engine.connect() as c:
        return str(c.execute(sa.select(catalog.work.c.kind).limit(1)).scalar_one())


def _mum_session(world: World) -> Any:
    client = world.env.other_client("10.0.0.33")
    assert world.env.login("mum", client=client).status_code == 200
    return client


def _shape(response: Any) -> tuple[Any, ...]:
    body = response.json()
    return response.status_code, body.get("type"), body.get("title"), body.get("detail")


def test_the_root_opens_a_door_on_the_selections(world: World) -> None:
    root = parse(world.kid.get("/opds"))
    doors = [
        x for e in entries(root) for x in links(e) if x.get("href", "").endswith("/opds/selections")
    ]
    assert len(doors) == 1  # a navigation entry, one per profile's feed
    assert "kind=navigation" in doors[0]["type"]
    again = parse(world.kid.get("/opds/selections"))
    assert titles(again) == []  # none yet: an empty list, not a refusal


def test_a_selection_is_one_entry_of_the_profile_that_made_it(world: World) -> None:
    mine = _make(world, world.kid_session, "Kid's tales", {"kind": [_kind(world)]})
    hers = _make(world, _mum_session(world), "Mum's tales", {"kind": [_kind(world)]})
    kid_feed = parse(world.kid.get("/opds/selections"))
    assert titles(kid_feed) == ["Kid's tales"]
    only = entries(kid_feed)[0]
    sub = next(x for x in links(only) if x["href"].endswith(f"/opds/selections/{mine}"))
    assert "kind=acquisition" in sub["type"]
    assert titles(parse(world.mum.get("/opds/selections"))) == ["Mum's tales"]
    assert hers not in world.kid.get("/opds/selections").text
    assert mine not in world.mum.get("/opds/selections").text


def test_a_selection_feed_is_the_scope_of_the_profile_that_reads_it(world: World) -> None:
    query = {"kind": [_kind(world)]}
    mine = _make(world, world.kid_session, "All", query)
    hers = _make(world, _mum_session(world), "All", query)
    kid = parse(world.kid.get(f"/opds/selections/{mine}"))
    assert sorted(titles(kid)) == work_titles(world, SEEN_KID)
    assert total(kid) == len(SEEN_KID)  # the total is made of what she may see
    world.clean(ET.tostring(kid, encoding="unicode"))
    mum = parse(world.mum.get(f"/opds/selections/{hers}?limit=50"))
    assert sorted(titles(mum)) == sorted({**VISIBLE_TITLES, **HIDDEN_TITLES}.values())
    assert total(mum) == len(VISIBLE_TITLES) + len(HIDDEN_TITLES)  # a member is not narrowed


def test_a_selection_feed_is_evaluated_at_each_request(world: World) -> None:
    mine = _make(world, world.kid_session, "All", {"kind": [_kind(world)]})
    assert world.seed.title_of("ok") in titles(parse(world.kid.get(f"/opds/selections/{mine}")))
    with world.seed.engine.begin() as conn:  # an admin re-rates the Work above her ceiling
        conn.execute(
            sa.update(catalog.work)
            .where(catalog.work.c.id == world.seed.works["ok"])
            .values(age_rating=16)
        )
    after = parse(world.kid.get(f"/opds/selections/{mine}"))
    assert world.seed.title_of("ok") not in titles(after)
    assert total(after) == len(SEEN_KID) - 1
    gone = world.kid_session.delete(
        f"{SELECTIONS}/{mine}", headers={CSRF_HEADER: world.env.csrf(world.kid_session)}
    )
    assert gone.status_code == 204
    assert world.kid.get(f"/opds/selections/{mine}").status_code == 404  # the very next request


def test_pages_of_a_selection_hold_visible_rows_only(world: World) -> None:
    mine = _make(world, world.kid_session, "All", {"kind": [_kind(world)]})
    path: str | None = f"/opds/selections/{mine}?limit=1"
    seen, totals, pages = [], [], 0
    while path is not None:
        feed = parse(world.kid.get(path))
        pages += 1
        assert pages < 10, "a page chain that never ends"
        seen += titles(feed)
        totals.append(total(feed))
        nxt = link(feed, "next")
        path = nxt["href"] if nxt else None
    assert sorted(seen) == work_titles(world, SEEN_KID) and pages == len(SEEN_KID)
    assert set(totals) == {len(SEEN_KID)}  # no page says more than the child may see
    past = parse(world.kid.get(f"/opds/selections/{mine}?limit=1&offset=3"))
    assert titles(past) == [] and link(past, "next") is None


def test_a_selection_names_nothing_the_child_may_not_see(
    world: World,
) -> None:
    closed = _make(world, world.kid_session, "Closed", {"shelf": [world.seed.shelves["family"]]})
    feed = parse(world.kid.get(f"/opds/selections/{closed}"))
    assert titles(feed) == [] and total(feed) == 0
    world.clean(ET.tostring(feed, encoding="unicode"))
    french = _make(world, world.kid_session, "French", {"lang": ["fr"]})
    feed = parse(world.kid.get(f"/opds/selections/{french}"))
    assert titles(feed) == [world.seed.title_of("ok")] and total(feed) == 1  # not `mixed`
    world.clean(ET.tostring(feed, encoding="unicode"))


def test_the_whole_graph_with_selections_holds_nothing_hidden_for_a_child(world: World) -> None:
    for query in ({"kind": [_kind(world)]}, {"shelf": [world.seed.shelves["adult"]]}):
        _make(world, world.kid_session, "q", query)
    seen, _hrefs = crawl(world.kid)
    assert any("/opds/selections/" in path for path in seen)  # the walk went through them
    for body in seen.values():
        world.clean(body)


def test_another_profiles_selection_is_one_that_does_not_exist(world: World) -> None:
    hers = _make(world, _mum_session(world), "Hers", {"kind": [_kind(world)]})
    mine = _make(world, world.kid_session, "Mine", {"kind": [_kind(world)]})
    absent = world.kid.get(f"/opds/selections/{ABSENT}")
    assert absent.status_code == 404
    assert _shape(world.kid.get(f"/opds/selections/{hers}")) == _shape(absent)
    assert _shape(world.mum.get(f"/opds/selections/{mine}")) == _shape(absent)
    assert world.kid.get("/opds/selections/not-an-id").status_code == 404


def test_a_selection_feed_opens_to_a_device_token_only(world: World) -> None:
    mine = _make(world, world.kid_session, "Mine", {"kind": [_kind(world)]})
    anonymous = world.env.other_client("10.0.0.98")
    for path in ("/opds/selections", f"/opds/selections/{mine}"):
        assert anonymous.get(path).status_code == 401
        for session in (world.kid_session, world.env.client):  # a web session opens nothing here
            got = session.get(path)
            assert got.status_code == 401, (path, got.status_code)
            world.clean(got.text)
