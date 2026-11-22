"""M6 L21 (EF-USR-020, EF-USR-006): a child's request that arrives lands on her reading shelf.

The debt of M4 L31, redated M6 by M5 L22b: the reading shelf made with an imported list held
what the child was shown at import, and a book she asked for that arrived later never landed on
it. At the arrival (the `unit.stored` subscriber, and the approval of a Work the library already
holds — the same door), the Work of a satisfied request of a child is added to the shelf of the
list its line came from, only if her age and the shelf still allow it: what she is shown, her
own shelf, the rule of the import. Otherwise the line stays « arrivée, à classer »: the request
is arrived, the shelf is not filled. A replay of the arrival adds nothing.

The attacks: a Work above her ceiling never lands (and never leaks), a shelf that is gone or no
longer hers refuses nothing, and the arrival itself never breaks because of the shelf.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.request_lists import RequestListService
from shelfhold.acquisition.application.requests import RequestService
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.ops import requests as ops_requests
from shelfhold.plugin_host.registry import PluginRegistry
from tests.auth_env import CSRF_HEADER, T0
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)
from tests.integration.test_schema_catalog import add_edition, add_unit, add_work
from tests.request_env import CHILD_ID, admin, rig
from tests.visibility_env import add_profile

LISTS = "/api/v1/request-lists"
COLLECTIONS = "/api/v1/collections"
REQUESTS = "/api/v1/requests"
RESERVED = "Tale of Reserved Shelf"  # rated 6, but its only Edition sits on a closed shelf
OVER_AGE = "Tale of Twelve Rivers"  # 12+, above her ceiling of 10


def _post(world: World, client: Any, path: str, body: Any = None) -> Any:
    return client.post(path, json=body, headers={CSRF_HEADER: world.env.csrf(client)})


def _rows(world: World, sql: str, **params: Any) -> list[Any]:
    with world.seed.engine.connect() as connection:
        return list(connection.execute(sa.text(sql), params))


def _kid_list(world: World, text: str, **extra: Any) -> Any:
    return _post(
        world, world.env.client, f"{LISTS}/for-child/{world.kid_id}", {"text": text, **extra}
    )


def _requests_of(world: World, batch_id: str) -> list[str]:
    """The request of each line that is not on the shelf yet, in the order of the file."""
    screen = world.env.client.get(f"{LISTS}/{batch_id}").json()
    asked = [str(i["request_id"]) for i in screen["items"] if not i["in_library"]]
    assert asked, "the import made no request"
    return asked


def _approve(world: World, request_id: str, body: dict[str, Any]) -> Any:
    assert world.env.login().status_code == 200  # a reserved action: a fresh admin session
    return _post(world, world.env.client, f"{REQUESTS}/{request_id}/approve", body)


def _shelf_items(world: World, collection_id: str) -> list[tuple[str, int]]:
    return [
        (str(r[0]), int(r[1]))
        for r in _rows(
            world,
            "SELECT work_id, position FROM collection_item WHERE collection_id = :i"
            " ORDER BY position",
            i=collection_id,
        )
    ]


# -- the arrival lands the Work on the child's shelf ----------------------------------------------


@pytest.mark.req("EF-USR-020", "EF-USR-006")
def test_child_request_lands_on_child_shelf_on_arrival(world: World) -> None:
    # her line names a Work the catalogue holds but she cannot see: its only Edition sits on a
    # shelf reserved to another profile, so the import made it a request, not a shelf line
    done = _kid_list(world, f"{RESERVED}\n", name="Arrival")
    assert done.status_code == 201, done.text
    batch = done.json()
    assert (batch["in_library"], batch["requested"]) == (0, 1)
    (rid,) = _requests_of(world, batch["id"])
    # the approval rates it and files its Edition on a shelf open to her: the book arrives
    approved = _approve(world, rid, {"age": 6, "shelf_id": world.seed.shelves["adult"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "arrived"
    # the Work lands on her reading shelf, at the place of its line
    assert _shelf_items(world, batch["collection_id"]) == [(world.seed.works["reserved"], 0)]
    # her shelf, read by her: the book is there, and only that one
    mine = world.kid.get(f"{COLLECTIONS}/{batch['collection_id']}")
    assert mine.status_code == 200 and mine.json()["mine"] is True
    assert [i["title"] for i in mine.json()["items"]] == [RESERVED]
    # and her request reads « arrived », not « not_found »: the book is hers now
    hers = world.kid.get(REQUESTS)
    assert [(r["id"], r["state"]) for r in hers.json()["items"]] == [(rid, "arrived")]


@pytest.mark.req("EF-USR-020", "EF-USR-006")
def test_child_request_over_age_stays_unshelved(world: World) -> None:
    # two lines of one list: a Work she will be shown when it arrives, and one above her
    # ceiling — the same approval, the same arrival, not the same shelf
    done = _kid_list(world, f"{RESERVED}\n{OVER_AGE}\n", name="Mixed")
    assert done.status_code == 201, done.text
    batch = done.json()
    assert (batch["in_library"], batch["requested"]) == (0, 2)
    lands, stays = _requests_of(world, batch["id"])
    approved = _approve(world, lands, {"age": 6, "shelf_id": world.seed.shelves["adult"]})
    assert approved.status_code == 200, approved.text
    # the honest age, above her ceiling: a warning at the preview, never a refusal
    over = _approve(world, stays, {"age": 12})
    assert over.status_code == 200, over.text
    assert over.json()["state"] == "arrived"
    # the first line lands, the over-age one stays « arrivée, à classer »
    assert _shelf_items(world, batch["collection_id"]) == [(world.seed.works["reserved"], 0)]
    mine = world.kid.get(f"{COLLECTIONS}/{batch['collection_id']}")
    assert mine.status_code == 200 and [i["title"] for i in mine.json()["items"]] == [RESERVED]
    # the admin's screen says both arrived; for her, the over-age one is exactly an absent
    # book: no state, no title, nothing of it (the other is hers now, title and all)
    screen = world.env.client.get(f"{LISTS}/{batch['id']}").json()
    assert [i["request_state"] for i in screen["items"]] == ["arrived", "arrived"]
    hers = {r["id"]: r for r in world.kid.get(REQUESTS).json()["items"]}
    assert hers[lands]["state"] == "arrived" and hers[lands]["title"] == RESERVED
    assert hers[stays]["state"] == "not_found" and hers[stays]["title"] is None


@pytest.mark.req("EF-USR-020", "EF-USR-010")
def test_arrival_replay_adds_once(settings: Settings) -> None:
    """Through the real ports and the real subscriber: the Work is rated at the approval, its
    Edition arrives with the Unit, and the `unit.stored` event — delivered twice, as a bus that
    does not promise otherwise — adds the shelf line once."""
    with rig(settings) as r:
        comp = SimpleNamespace(
            engine=r.engine, clock=r.clock, bus=EventBus(r.clock),
            plugins=lambda: PluginRegistry(), settings=SimpleNamespace(fresh_auth_s=900),
        )  # fmt: skip
        service = RequestService(r.engine, r.clock, ops_requests.build_ports(comp), bus=comp.bus)
        ops_requests.subscribe_arrivals(comp, service)
        lists = RequestListService(r.engine, r.clock, service)
        with write_transaction(r.engine) as connection:
            shelf = new_ulid()
            connection.execute(sa.insert(export.shelf).values(id=shelf, name="Open", phrase="open"))
            work = add_work(
                connection, title="Tale of a Late Arrival", sort_title="tale of a late arrival"
            )
        done = lists.import_list(admin(), "Tale of a Late Arrival\n", for_user_id=CHILD_ID)
        assert (done.in_library, done.requested) == (0, 1)
        (rid,) = [str(row[0]) for row in r.rows("SELECT request_id FROM request_list_item")]
        # the approval rates the Work; its Edition does not exist yet, so the request waits
        assert service.approve(rid, admin(), age=0).state == "approved"  # a tier, under her ceiling
        with write_transaction(r.engine) as connection:
            edition = add_edition(connection, work, age_rating=0, rated_at=T0, shelf_id=shelf)
            unit = add_unit(connection, edition)
            first = comp.bus.publish(
                connection, "unit.stored",
                {"unit_id": unit, "edition_id": edition, "work_id": work, "kind": "cbz"},
            )  # fmt: skip
        comp.bus.deliver([first])
        assert r.state_of(rid) == "arrived"
        assert r.rows("SELECT work_id, position FROM collection_item") == [(work, 0)]
        # the event again: the line is already there, nothing is added a second time
        with write_transaction(r.engine) as connection:
            replay = comp.bus.publish(
                connection, "unit.stored",
                {"unit_id": unit, "edition_id": edition, "work_id": work, "kind": "cbz"},
            )  # fmt: skip
        comp.bus.deliver([replay])
        assert r.state_of(rid) == "arrived"
        assert r.rows("SELECT work_id, position FROM collection_item") == [(work, 0)]


# -- the shelf itself can refuse ------------------------------------------------------------------


@pytest.mark.req("EF-USR-020")
def test_an_arrival_whose_shelf_is_gone_lands_nowhere_and_breaks_nothing(
    world: World,
) -> None:
    # the same wanted book in two lists: the shelf of the first was deleted since, the second
    # is still hers — the arrival lands on the one that is left, and never breaks
    gone = _kid_list(world, f"{RESERVED}\n", name="Gone")
    kept = _kid_list(world, f"{RESERVED}\n", name="Kept")
    assert gone.status_code == kept.status_code == 201, (gone.text, kept.text)
    (rid,) = _requests_of(world, gone.json()["id"])  # one request, two lines
    assert _requests_of(world, kept.json()["id"]) == [rid]
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.text("DELETE FROM collection WHERE id = :c"), {"c": gone.json()["collection_id"]}
        )
    approved = _approve(world, rid, {"age": 6, "shelf_id": world.seed.shelves["adult"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "arrived"
    # the link went with the shelf (SET NULL); the line of the other list lands as ever
    assert _rows(
        world, "SELECT collection_id FROM request_list_import WHERE id = :i",
        i=gone.json()["id"],
    ) == [(None,)]  # fmt: skip
    assert _shelf_items(world, kept.json()["collection_id"]) == [(world.seed.works["reserved"], 0)]
    assert _rows(
        world,
        "SELECT COUNT(*) FROM collection_item WHERE work_id = :w",
        w=world.seed.works["reserved"],
    ) == [(1,)]


@pytest.mark.req("EF-USR-020", "EF-USR-006")
def test_nothing_lands_on_a_shelf_that_is_no_longer_hers(world: World) -> None:
    # a shelf transferred to another profile is not the child's anymore: her line's book does
    # not land on it, and lands on the other list's shelf, which is still hers
    add_profile(world.env, "ann", "member", "10.0.0.11")
    ann_id = str(_rows(world, "SELECT id FROM user WHERE username = 'ann'")[0][0])
    taken = _kid_list(world, f"{RESERVED}\n", name="Taken")
    kept = _kid_list(world, f"{RESERVED}\n", name="Kept")
    assert taken.status_code == kept.status_code == 201, (taken.text, kept.text)
    (rid,) = _requests_of(world, taken.json()["id"])
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE collection SET owner_id = :o WHERE id = :c"),
            {"o": ann_id, "c": taken.json()["collection_id"]},
        )
    approved = _approve(world, rid, {"age": 6, "shelf_id": world.seed.shelves["adult"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "arrived"
    assert _shelf_items(world, taken.json()["collection_id"]) == []
    assert _shelf_items(world, kept.json()["collection_id"]) == [(world.seed.works["reserved"], 0)]
