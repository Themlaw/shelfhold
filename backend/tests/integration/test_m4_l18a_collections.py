"""M4 L18a (EF-BIB-016, EF-USR-006, ENF-SEC-010): collections and reading lists, read as a profile.

A collection (the household's) or a reading list (a profile's own, private by default) holds Works
in an order. What a reader is shown of one is made of the Works she may see, nothing else: not the
count, not the order, not the cover, not the title when they would tell of a hidden Work. The CBL
import keeps the order of the file and lists the lines it could not resolve, never inventing one.
Real application, real sessions, real keys.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.auth_env import CSRF_HEADER
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    MISSING,
    World,
    api,
    world,
)
from tests.thumbnail_env import point_at
from tests.visibility_env import RATED, add_profile

COLLECTIONS = "/api/v1/collections"
IMPORT = "/api/v1/collections/import-cbl"
SECRET_FILE = "root:x:0:0"  # noqa: S105  (what /etc/passwd starts with, not a secret)


def _cbl(books: list[tuple[str, str]], name: str = "Reading order") -> str:
    rows = "".join(f'<Book Series="{s}" Number="{n}" Volume="2020" Year="2021"/>' for s, n in books)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<ReadingList><Name>{name}</Name><Books>{rows}</Books></ReadingList>"
    )


def _post(world: World, client: Any, path: str, body: Any = None) -> Any:
    return client.post(path, json=body, headers={CSRF_HEADER: world.env.csrf(client)})


def _delete(world: World, client: Any, path: str) -> Any:
    return client.delete(path, headers={CSRF_HEADER: world.env.csrf(client)})


def _make(world: World, client: Any, name: str, scope: str, works: Sequence[str] = ()) -> str:
    made = _post(world, client, COLLECTIONS, {"name": name, "kind": "reading_list", "scope": scope})
    assert made.status_code == 201, made.text
    collection = made.json()["id"]
    for work in works:
        added = _post(world, client, f"{COLLECTIONS}/{collection}/items", {"work_id": work})
        assert added.status_code == 201, added.text
    return collection


def _force_items(world: World, collection: str, works: list[str]) -> None:
    """Items written under the filter's feet (a rating changed later, a Work added by an admin)."""
    with world.seed.engine.begin() as c:
        base = c.execute(
            sa.select(sa.func.coalesce(sa.func.max(catalog.collection_item.c.position), -1)).where(
                catalog.collection_item.c.collection_id == collection
            )
        ).scalar_one()
        for offset, work in enumerate(works, start=1):
            c.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=collection, position=base + offset, work_id=work
                )
            )


def _titles(answer: Any) -> list[str]:
    return [item["title"] for item in answer.json()["items"]]


def _key_client(world: World, username: str, ip: str, **kwargs: Any) -> tuple[Any, dict[str, str]]:
    key = local_auth(world.seed.engine, world.env.settings).create_api_key(
        name=f"{username}-key", username=username, **kwargs
    )
    return world.env.other_client(ip), {"Authorization": f"Bearer {key.value}"}


# -- the import keeps the order -------------------------------------------------------------------


@pytest.mark.req("EF-BIB-016")
def test_cbl_import_keeps_order(world: World) -> None:
    works = world.seed.works
    document = _cbl(
        [
            ("Tale of Mixed Editions", "1"),
            ("Tale of Orchard", "2"),
            ("A Tale Nobody Catalogued", "7"),  # not in the catalogue: listed, never invented
            ("tale  of QUIET shelf", "3"),  # the same title, written differently
            ("Tale of Orchard", "4"),  # the same Work twice keeps both places
        ]
    )
    done = _post(world, world.env.client, IMPORT, {"scope": "shared", "cbl": document})
    assert done.status_code == 201, done.text
    body = done.json()
    assert body["imported"] == 4
    assert [(u["index"], u["series"], u["number"]) for u in body["unresolved"]] == [
        (2, "A Tale Nobody Catalogued", "7")
    ]
    assert body["unresolved"][0]["reason"] == "not_found"
    shown = world.env.client.get(f"{COLLECTIONS}/{body['collection']['id']}")
    assert shown.status_code == 200
    assert [i["work_id"] for i in shown.json()["items"]] == [
        works["mixed"],
        works["ok"],
        works["adult_shelf"],
        works["ok"],
    ]
    assert [i["number"] for i in shown.json()["items"]] == ["1", "2", "3", "4"]
    assert [i["position"] for i in shown.json()["items"]] == [1, 2, 3, 4]
    assert shown.json()["name"] == "Reading order"
    assert shown.json()["count"] == 4


@pytest.mark.req("EF-BIB-016")
def test_cbl_import_does_not_guess_between_two_works_of_one_title(world: World) -> None:
    with world.seed.engine.begin() as c:
        kind = c.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one()
        c.execute(
            sa.insert(catalog.work).values(
                id=new_ulid(), kind=kind, title="Tale of Orchard", sort_title="tale of orchard",
                age_rating=6, rated_at=RATED,
            )
        )  # fmt: skip
    done = _post(
        world,
        world.env.client,
        IMPORT,
        {"scope": "shared", "cbl": _cbl([("Tale of Orchard", "1")])},
    )
    assert done.status_code == 201
    assert done.json()["imported"] == 0
    assert done.json()["unresolved"][0]["reason"] == "ambiguous"


@pytest.mark.req("EF-BIB-016", "ENF-SEC-014")
def test_a_trapped_cbl_is_refused_and_creates_nothing(world: World) -> None:
    trapped = (
        '<?xml version="1.0"?><!DOCTYPE ReadingList [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
        "<ReadingList><Name>&leak;</Name><Books/></ReadingList>"
    )
    too_big = _cbl([("Tale", str(i)) for i in range(6000)])
    deep = (
        '<?xml version="1.0"?><ReadingList><Name>x</Name><Books>'
        + "<a>" * 80
        + "</a>" * 80
        + "</Books></ReadingList>"
    )
    for document in (trapped, too_big, deep, "", "not xml at all"):
        refused = _post(world, world.env.client, IMPORT, {"scope": "shared", "cbl": document})
        assert refused.status_code == 422, document[:40]
        assert SECRET_FILE not in refused.text
    assert world.env.client.get(COLLECTIONS).json()["items"] == []


@pytest.mark.req("EF-BIB-016", "ENF-SEC-010")
def test_a_child_import_resolves_only_what_she_may_see(world: World) -> None:
    document = _cbl(
        [("Tale of Orchard", "1"), ("Tale of Harbor Sixteen", "2"), ("Tale of Xylophone", "3")]
    )
    done = _post(world, world.kid, IMPORT, {"scope": "profile", "cbl": document})
    assert done.status_code == 201, done.text
    assert done.json()["imported"] == 1
    # a hidden Work is exactly an absent one: not found, with no hint that it exists
    assert {u["reason"] for u in done.json()["unresolved"]} == {"not_found"}
    assert done.json()["unresolved"][0]["index"] == 1


# -- a profile's list is private ------------------------------------------------------------------


@pytest.mark.req("EF-BIB-016", "ENF-SEC-010")
def test_profile_list_invisible_to_other_profiles(world: World) -> None:
    works = world.seed.works
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    bob = add_profile(world.env, "bob", "member", "10.0.0.12")
    mine = _make_as(world, ann, "Ann's private list", "profile", [works["ok"]])
    path = f"{COLLECTIONS}/{mine}"
    # the owner and the admin read it
    assert ann.get(path).status_code == 200
    assert world.env.client.get(path).status_code == 200
    # another member, a child, a guessed identifier: the same answer as for one nobody ever had
    absent = bob.get(f"{COLLECTIONS}/{MISSING}")
    assert absent.status_code == 404
    for stranger in (bob, world.kid):
        answer = stranger.get(path)
        assert (answer.status_code, answer.json()["type"]) == (404, absent.json()["type"])
        assert answer.json().get("detail") == absent.json().get("detail")
        assert "Ann's private list" not in answer.text
        listed = stranger.get(COLLECTIONS)
        assert listed.status_code == 200 and "Ann's private list" not in listed.text
        assert mine not in listed.text
        # nothing of hers can be written through her identifier
        item_path = f"{path}/items"
        assert _post(world, stranger, item_path, {"work_id": works["mixed"]}).status_code == 404
        assert _delete(world, stranger, path).status_code == 404
    assert [i["work_id"] for i in ann.get(path).json()["items"]] == [works["ok"]]
    item = ann.get(path).json()["items"][0]["id"]
    assert _delete(world, bob, f"{path}/items/{item}").status_code == 404
    assert len(ann.get(path).json()["items"]) == 1
    # the list she makes is hers: the admin sees it among all, a member only among her own
    assert mine in world.env.client.get(COLLECTIONS).text
    assert mine in ann.get(COLLECTIONS).text


def _make_as(world: World, client: Any, name: str, scope: str, works: list[str]) -> str:
    return _make(world, client, name, scope, works)


@pytest.mark.req("EF-BIB-016", "ENF-SEC-010")
def test_a_key_reads_as_its_profile_and_a_read_only_key_writes_nothing(world: World) -> None:
    works = world.seed.works
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    add_profile(world.env, "bob", "member", "10.0.0.12")
    mine = _make(world, ann, "Ann's private list", "profile", [works["ok"]])
    shared = _make(world, world.env.client, "Household", "shared", [works["ok"]])
    bob_client, bob_auth = _key_client(world, "bob", "10.0.0.61", read_only=True)
    ann_client, ann_auth = _key_client(world, "ann", "10.0.0.62", read_only=True)
    # Bob's key reads as Bob: not Ann's list, by identifier or by listing
    assert bob_client.get(f"{COLLECTIONS}/{mine}", headers=bob_auth).status_code == 404
    assert mine not in bob_client.get(COLLECTIONS, headers=bob_auth).text
    assert bob_client.get(f"{COLLECTIONS}/{shared}", headers=bob_auth).status_code == 200
    # Ann's own key reads her list
    assert ann_client.get(f"{COLLECTIONS}/{mine}", headers=ann_auth).status_code == 200
    # a read-only key writes nothing, anywhere
    for client, auth, path, body in (
        (
            ann_client,
            ann_auth,
            COLLECTIONS,
            {"name": "x", "kind": "reading_list", "scope": "profile"},
        ),
        (ann_client, ann_auth, f"{COLLECTIONS}/{mine}/items", {"work_id": works["mixed"]}),
        (
            ann_client,
            ann_auth,
            IMPORT,
            {"scope": "profile", "cbl": _cbl([("Tale of Orchard", "1")])},
        ),
    ):
        refused = client.post(path, json=body, headers=auth)
        assert refused.status_code in (401, 403), path
    assert ann_client.delete(f"{COLLECTIONS}/{mine}", headers=ann_auth).status_code in (401, 403)
    assert len(ann.get(f"{COLLECTIONS}/{mine}").json()["items"]) == 1
    # and an anonymous caller gets nothing
    anonymous = world.env.other_client("10.0.0.63")
    assert anonymous.get(COLLECTIONS).status_code == 401
    assert anonymous.get(f"{COLLECTIONS}/{shared}").status_code == 401


@pytest.mark.req("EF-BIB-016", "ENF-SEC-010")
def test_only_an_admin_makes_a_household_collection(world: World) -> None:
    works = world.seed.works
    member = add_profile(world.env, "mem", "member", "10.0.0.13")
    for client in (member, world.kid):
        refused = _post(
            world, client, COLLECTIONS, {"name": "x", "kind": "collection", "scope": "shared"}
        )
        assert refused.status_code == 403
        imported = _post(
            world, client, IMPORT, {"scope": "shared", "cbl": _cbl([("Tale of Orchard", "1")])}
        )
        assert imported.status_code == 403
    shared = _make(world, world.env.client, "Household", "shared", [works["ok"]])
    for client in (member, world.kid):  # everyone reads it, nobody but an admin changes it
        assert client.get(f"{COLLECTIONS}/{shared}").status_code == 200
        assert (
            _post(
                world, client, f"{COLLECTIONS}/{shared}/items", {"work_id": works["mixed"]}
            ).status_code
            == 403
        )
        assert _delete(world, client, f"{COLLECTIONS}/{shared}").status_code == 403
    assert len(world.env.client.get(f"{COLLECTIONS}/{shared}").json()["items"]) == 1


# -- a child sees only what she may see -----------------------------------------------------------


@pytest.mark.req("EF-BIB-016", "EF-USR-006", "ENF-SEC-010")
def test_child_sees_only_collection_items_visible_to_her(world: World) -> None:
    works, ids = world.seed.works, world.seed.works
    shared = _make(world, world.env.client, "Tale of Harbor Sixteen: the long way", "shared")
    _force_items(
        world, shared,
        [ids["above"], ids["ok"], ids["x"], ids["unrated"], ids["adult_shelf"], ids["flagged"],
         ids["orphan"], ids["mixed"], ids["twelve"]],
    )  # fmt: skip
    admin_view = world.env.client.get(f"{COLLECTIONS}/{shared}")
    assert admin_view.json()["count"] == 9  # the admin sees all of it
    answer = world.kid.get(f"{COLLECTIONS}/{shared}")
    assert answer.status_code == 200
    body = answer.json()
    assert [i["work_id"] for i in body["items"]] == [
        works["ok"],
        works["adult_shelf"],
        works["mixed"],
    ]
    assert body["count"] == 3  # a count of what she sees: it cannot say 6 more exist
    assert [i["position"] for i in body["items"]] == [1, 2, 3]  # no gap where a hidden one was
    world.clean(answer.text)
    # the name of a list that holds a hidden Work could spoil it: withheld, with no other word
    assert body["name"] is None and "Harbor" not in answer.text
    listed = world.kid.get(COLLECTIONS)
    assert listed.status_code == 200
    row = next(r for r in listed.json()["items"] if r["id"] == shared)
    assert row["count"] == 3 and row["name"] is None
    world.clean(listed.text)
    # a list with nothing hidden keeps its name
    plain = _make(world, world.env.client, "Cosy reads", "shared", [works["ok"], works["mixed"]])
    row = next(r for r in world.kid.get(COLLECTIONS).json()["items"] if r["id"] == plain)
    assert row["name"] == "Cosy reads" and row["count"] == 2
    # a list that holds only hidden Works does not exist for her
    closed = _make(world, world.env.client, "Closed", "shared")
    _force_items(world, closed, [ids["above"], ids["x"]])
    assert world.kid.get(f"{COLLECTIONS}/{closed}").status_code == 404
    assert closed not in world.kid.get(COLLECTIONS).text
    absent = world.kid.get(f"{COLLECTIONS}/{MISSING}")
    assert world.kid.get(f"{COLLECTIONS}/{closed}").json()["type"] == absent.json()["type"]


@pytest.mark.req("EF-BIB-016", "EF-USR-006", "ENF-SEC-010")
def test_the_cover_of_a_list_is_never_a_hidden_works(world: World) -> None:
    works, editions = world.seed.works, world.seed.editions
    hidden_cover = f"covers/{'a' * 64}.webp"
    shown_cover = f"covers/{'b' * 64}.webp"
    reserved_cover = f"covers/{'c' * 64}.webp"
    with world.seed.engine.begin() as c:
        point_at(c, editions["above"], hidden_cover)  # a Work above her ceiling
        point_at(c, editions["ok_second"], shown_cover)  # an Edition on a shelf open to her
        point_at(c, editions["ok"], reserved_cover)  # an Edition on a shelf reserved to another
    shared = _make(world, world.env.client, "Covers", "shared")
    _force_items(world, shared, [works["above"], works["ok"]])  # the hidden one comes first
    admin = world.env.client.get(f"{COLLECTIONS}/{shared}").json()
    kid = world.kid.get(f"{COLLECTIONS}/{shared}").json()
    assert admin["cover_hash"] == "a" * 64  # the admin's first item is the hidden one
    assert kid["cover_hash"] == "b" * 64
    assert "a" * 64 not in json.dumps(kid) and "c" * 64 not in json.dumps(kid)
    listed = world.kid.get(COLLECTIONS).json()["items"]
    assert next(r for r in listed if r["id"] == shared)["cover_hash"] == kid["cover_hash"]
    assert "a" * 64 not in json.dumps(listed) and "c" * 64 not in json.dumps(listed)


@pytest.mark.req("EF-BIB-016", "EF-USR-006", "ENF-SEC-010")
def test_a_work_outside_the_shelves_open_to_her_is_not_shown(world: World) -> None:
    """The perimeter of the shelves (`shelf.allowed_profiles`): a Work whose every Edition she may
    see is filed only on a shelf reserved to another profile is not hers, ceiling or not."""
    with world.seed.engine.begin() as c:
        kind = c.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one()
        reserved = new_ulid()
        c.execute(
            sa.insert(export.shelf).values(
                id=reserved, name="Reserved", phrase="reserved", max_age_rating=13,
                allowed_profiles=[new_ulid()],
            )
        )  # fmt: skip
        outside, loose = new_ulid(), new_ulid()
        for work, shelf, title in (
            (outside, reserved, "Tale of Reserved Shelf"),
            (loose, None, "Tale of No Shelf"),
        ):
            c.execute(
                sa.insert(catalog.work).values(
                    id=work, kind=kind, title=title, sort_title=title.lower(),
                    age_rating=6, rated_at=RATED,
                )
            )  # fmt: skip
            c.execute(
                sa.insert(catalog.edition).values(
                    id=new_ulid(), work_id=work, lang="en", translation_type="official",
                    media="images", age_rating=None, rated_at=RATED, shelf_id=shelf,
                )
            )  # fmt: skip
    shared = _make(world, world.env.client, "Perimeter", "shared")
    _force_items(world, shared, [outside, loose, world.seed.works["ok"]])
    admin = world.env.client.get(f"{COLLECTIONS}/{shared}")
    assert _titles(admin) == ["Tale of Reserved Shelf", "Tale of No Shelf", "Tale of Orchard"]
    kid = world.kid.get(f"{COLLECTIONS}/{shared}")
    assert _titles(kid) == ["Tale of Orchard"]
    assert "Reserved Shelf" not in kid.text and "No Shelf" not in kid.text
    # an adult member is not bound by a shelf reserved to someone else (as everywhere in the app)
    member = add_profile(world.env, "mem", "member", "10.0.0.14")
    assert len(member.get(f"{COLLECTIONS}/{shared}").json()["items"]) == 3
    # nor can a child add it to a list of hers
    mine = _make(world, world.kid, "Mine", "profile")
    assert (
        _post(world, world.kid, f"{COLLECTIONS}/{mine}/items", {"work_id": outside}).status_code
        == 404
    )


@pytest.mark.req("EF-BIB-016", "EF-USR-006", "ENF-SEC-010")
def test_a_child_cannot_put_a_hidden_work_in_her_list_and_a_later_rating_hides_it(
    world: World,
) -> None:
    works = world.seed.works
    mine = _make(world, world.kid, "Mine", "profile", [works["ok"]])
    path = f"{COLLECTIONS}/{mine}/items"
    absent = _post(world, world.kid, path, {"work_id": MISSING})
    assert absent.status_code == 404
    for key in ("above", "x", "unrated", "flagged", "orphan", "twelve"):
        refused = _post(world, world.kid, path, {"work_id": works[key]})
        assert refused.status_code == 404, key
        assert refused.json()["type"] == absent.json()["type"]
        assert refused.json().get("detail") == absent.json().get("detail")
    assert len(world.kid.get(f"{COLLECTIONS}/{mine}").json()["items"]) == 1
    # the admin raises the Work's age after she added it: it leaves what she is shown
    with world.seed.engine.begin() as c:
        c.execute(
            sa.update(catalog.work).where(catalog.work.c.id == works["ok"]).values(age_rating=16)
        )
    after = world.kid.get(f"{COLLECTIONS}/{mine}")
    assert after.status_code == 200 and after.json()["items"] == [] and after.json()["count"] == 0
    # a list she owns, once empty for her, is still hers (not a 404): she made it
    assert world.kid.get(COLLECTIONS).json()["items"][0]["count"] == 0


@pytest.mark.req("EF-BIB-016", "ENF-SEC-010")
def test_a_childs_list_is_read_by_her_and_the_admin_only(world: World) -> None:
    works = world.seed.works
    mine = _make(world, world.kid, "Kid list", "profile", [works["ok"]])
    other = add_profile(world.env, "kid2", "child", "10.0.0.15", max_age_rating=10)
    assert other.get(f"{COLLECTIONS}/{mine}").status_code == 404
    assert world.kid.get(f"{COLLECTIONS}/{mine}").status_code == 200
    assert world.env.client.get(f"{COLLECTIONS}/{mine}").status_code == 200
    # a household collection she cannot reach is not made reachable by being in her list
    assert _delete(world, other, f"{COLLECTIONS}/{mine}").status_code == 404
    assert _delete(world, world.kid, f"{COLLECTIONS}/{mine}").status_code == 204
    assert world.kid.get(f"{COLLECTIONS}/{mine}").status_code == 404


@pytest.mark.req("EF-BIB-016")
def test_removing_a_work_removes_its_items(world: World) -> None:
    works = world.seed.works
    alone = new_ulid()
    with world.seed.engine.begin() as c:
        kind = c.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one()
        c.execute(
            sa.insert(catalog.work).values(
                id=alone, kind=kind, title="Tale of Nobody", sort_title="tale of nobody",
                age_rating=6, rated_at=RATED,
            )
        )  # fmt: skip
    shared = _make(world, world.env.client, "Household", "shared", [works["ok"], alone])
    with world.seed.engine.begin() as c:
        c.execute(sa.delete(catalog.work).where(catalog.work.c.id == alone))
    shown = world.env.client.get(f"{COLLECTIONS}/{shared}").json()["items"]
    assert [i["work_id"] for i in shown] == [works["ok"]]


@pytest.mark.req("EF-BIB-016")
def test_a_merged_work_keeps_its_place_in_the_lists(world: World) -> None:
    """Merging two Works moves the place of the absorbed one onto the survivor, in the same
    order: a list does not lose a line because the catalogue was tidied."""
    from shelfhold.catalog.application.work_merge import move_collection_items

    works = world.seed.works
    shared = _make(world, world.env.client, "Household", "shared", [works["ok"], works["mixed"]])
    with world.seed.engine.begin() as c:
        move_collection_items(c, drop_id=works["ok"], keep_id=works["adult_shelf"])
    shown = world.env.client.get(f"{COLLECTIONS}/{shared}").json()["items"]
    assert [i["work_id"] for i in shown] == [works["adult_shelf"], works["mixed"]]


def child_case(world: World) -> None:
    """The surface `collections` of the child-surface suite (`test_child_surfaces`): everything she
    is shown of a list is made of Works she may see, and what she cannot see is an absent one."""
    works = world.seed.works
    shared = _make(world, world.env.client, "Tale of Harbor Sixteen: sequel", "shared")
    _force_items(world, shared, [works[k] for k in ("above", "x", "unrated", "ok", "mixed")])
    closed = _make(world, world.env.client, "Closed", "shared")
    _force_items(world, closed, [works["above"], works["flagged"]])
    answer = world.kid.get(f"{COLLECTIONS}/{shared}")
    assert answer.status_code == 200 and answer.json()["count"] == 2
    world.clean(answer.text)
    listed = world.kid.get(COLLECTIONS)
    world.clean(listed.text)
    assert closed not in listed.text and world.kid.get(f"{COLLECTIONS}/{closed}").status_code == 404
    mine = _make(world, world.kid, "Mine", "profile", [works["ok"]])
    assert (
        _post(
            world, world.kid, f"{COLLECTIONS}/{mine}/items", {"work_id": works["above"]}
        ).status_code
        == 404
    )
    imported = _post(
        world, world.kid, IMPORT,
        {
            "scope": "profile",
            "cbl": _cbl([("Tale of Harbor Sixteen", "1"), ("Tale of Orchard", "2")]),
        },
    )  # fmt: skip
    assert imported.status_code == 201 and imported.json()["imported"] == 1
    # the only place her own hidden title comes back is the echo of the line she sent, as
    # "not found": nothing of the catalogue is in the rest of the answer
    body = imported.json()
    assert [u["reason"] for u in body.pop("unresolved")] == ["not_found"]
    world.clean(json.dumps(body))
