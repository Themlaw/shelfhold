"""API-1 (EF-USR-015, ENF-SEC-010): `GET /api/v1/works` gains `q` and the reading id of each item.

`q` is the words of a title or of an author, folded for case (and accents where the index folds
them); it is one more condition on the one filter, so a child finds nothing above her ceiling and
the total counts nothing she may not see. `read_unit_id` is the Unit a non-admin opens to read:
the one under way, else the first with a file, in an Edition she may see.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import index_work, rebuild_search_index
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog
from tests.integration.test_l05a_content_route import World
from tests.integration.test_m5_l06_publication import unit_of
from tests.integration.test_m5_l07_positions import kept, page, put
from tests.integration.test_m5_l14_continue_reading import world  # noqa: F401

pytestmark = pytest.mark.req("EF-USR-015", "ENF-SEC-010")

WORKS = "/api/v1/works"


@pytest.fixture(autouse=True)
def indexed(world: World) -> None:  # noqa: F811
    """The seed is written by SQL: the search index is made from it, as the maintenance job does."""
    with write_transaction(world.seed.engine) as connection:
        rebuild_search_index(connection)


def listing(client: Any, **params: Any) -> dict[str, Any]:
    got = client.get(WORKS, params={"limit": 200, **params})
    assert got.status_code == 200, got.text
    return dict(got.json())


def ids(client: Any, **params: Any) -> set[str]:
    return {i["work_id"] for i in listing(client, **params)["items"]}


def _author(world: World, work: str, name: str) -> None:  # noqa: F811
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(catalog.metadata_field).values(
                entity_type="work", entity_id=work, field="contributors", lang="und",
                value=json.dumps([name]), provider="source-a", provenance="provider",
                fetched_at=datetime(2026, 11, 25, 9, 0, tzinfo=UTC),
            )
        )  # fmt: skip
        index_work(connection, work)


def test_q_finds_by_title_whatever_the_case(world: World) -> None:  # noqa: F811
    seed = world.seed
    assert ids(world.admin, q="orchard") == {seed.works["ok"]}
    assert ids(world.admin, q="ORCHARD") == {seed.works["ok"]}
    assert ids(world.admin, q="tale of twelve") == {seed.works["twelve"]}
    assert listing(world.admin, q="orchard")["total"] == 1


def test_q_without_a_word_is_no_filter_and_a_word_nobody_has_finds_nothing(
    world: World,  # noqa: F811
) -> None:
    assert ids(world.admin, q="   ") == ids(world.admin)
    nothing = listing(world.admin, q="zzzqqq")
    assert nothing["items"] == [] and nothing["total"] == 0


def test_q_finds_by_author_and_folds_the_accents_of_the_index(world: World) -> None:  # noqa: F811
    _author(world, world.seed.works["ok"], "Jules Verne")
    assert ids(world.admin, q="verne") == {world.seed.works["ok"]}
    assert ids(world.admin, q="tâlé") == ids(world.admin, q="tale")  # accents fold on titles


def test_q_composes_with_the_filters_and_the_paging(world: World) -> None:  # noqa: F811
    got = listing(world.admin, q="tale", limit=1, offset=1)
    assert len(got["items"]) == 1 and got["total"] == len(ids(world.admin, q="tale"))
    shelf = world.seed.shelves["family"]
    assert ids(world.admin, q="tale", shelf_id=shelf) <= ids(world.admin, q="tale")


def test_q_never_shows_a_child_a_work_above_her_ceiling(world: World) -> None:  # noqa: F811
    seed = world.seed
    found = listing(world.kid, q="tale")
    assert {i["work_id"] for i in found["items"]} == seed.visible_ids
    assert found["total"] == len(found["items"])  # no counter that says more than the page does
    for hidden in ("above", "x", "flagged", "adult_edition", "reserved"):
        assert seed.title_of(hidden) not in json.dumps(found)
        assert ids(world.kid, q=seed.title_of(hidden)) == set()
        assert listing(world.kid, q=seed.title_of(hidden))["total"] == 0
    # the author of a hidden work is no way in either
    _author(world, seed.works["above"], "Hidden Writer")
    assert listing(world.kid, q="hidden writer")["total"] == 0
    assert "Hidden Writer" not in world.kid.get(WORKS, params={"q": "hidden"}).text


def test_a_child_gets_the_same_answer_for_a_hidden_work_and_an_absent_one(
    world: World,  # noqa: F811
) -> None:
    hidden = listing(world.kid, q=world.seed.title_of("above"))
    absent = listing(world.kid, q="Nothing Of The Sort Exists")
    assert {k: v for k, v in hidden.items() if k != "since"} == {
        k: v for k, v in absent.items() if k != "since"
    }


def test_every_item_names_the_unit_that_opens_the_reading(world: World) -> None:  # noqa: F811
    seed = world.seed
    items = {i["work_id"]: i for i in listing(world.admin)["items"]}
    first = unit_of(world, world.open_item)
    assert items[seed.works["ok"]]["read_unit_id"] == first
    assert items[seed.works["above"]]["read_unit_id"] is None  # no file: nothing to open


def test_the_reading_id_is_the_unit_under_way_else_the_first(world: World) -> None:  # noqa: F811
    seed = world.seed
    first = unit_of(world, world.open_item)
    _second_file, second = kept(world)
    third_file, third = kept(world)
    put(world, world.admin, third_file, page(4), minutes=-1)
    items = {i["work_id"]: i for i in listing(world.admin)["items"]}
    assert items[seed.works["ok"]]["read_unit_id"] == third  # the one being read
    assert first not in (second, third)
    kid_items = {i["work_id"]: i for i in listing(world.kid)["items"]}
    assert kid_items[seed.works["ok"]]["read_unit_id"] == first  # her own progress is hers alone


def test_a_child_is_given_no_reading_id_of_a_unit_she_may_not_see(world: World) -> None:  # noqa: F811
    seed = world.seed
    hidden_file, hidden_unit = kept(
        world, edition="adult_edition"
    )  # a visible Work, a hidden Edition
    answer = world.kid.get(WORKS, params={"limit": 200})
    assert hidden_unit not in answer.text and hidden_file not in answer.text
    items = {i["work_id"]: i for i in answer.json()["items"]}
    assert seed.works["adult_edition"] not in items
    # the Work that holds both a visible and a reserved Edition names only the visible one's Unit
    _reserved_file, reserved_unit = kept(world, edition="ok_reserved")
    assert reserved_unit not in world.kid.get(WORKS, params={"limit": 200}).text


def test_the_reading_id_opens_the_reading_for_a_child(world: World) -> None:  # noqa: F811
    seed = world.seed
    content, unit = kept(world)
    put(world, world.kid, content, page(4), minutes=-1)
    item = next(i for i in listing(world.kid)["items"] if i["work_id"] == seed.works["ok"])
    assert item["read_unit_id"] == unit
    opened = world.kid.get(f"/api/reader/unit/{item['read_unit_id']}/publication")
    assert opened.status_code == 200, opened.text


# -- the authors named on an Edition, and how the value is read ----------------------------------


def _credit(world: World, entity: str, entity_id: str, value: object) -> None:  # noqa: F811
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(catalog.metadata_field).values(
                entity_type=entity, entity_id=entity_id, field="contributors", lang="und",
                value=json.dumps(value), provider="source-a", provenance="provider",
                fetched_at=datetime(2026, 11, 25, 9, 0, tzinfo=UTC),
            )
        )  # fmt: skip


@pytest.mark.parametrize(
    ("hidden_edition", "work_key"), [("ok_reserved", "ok"), ("mixed_new", "mixed")]
)
def test_q_never_reads_the_authors_of_an_edition_she_may_not_see(
    world: World,  # noqa: F811
    hidden_edition: str,
    work_key: str,
) -> None:
    """A Work is visible as soon as one Edition is: the authors named on another Edition (reserved
    to another profile, or still unclassified) are not hers, so no word of them finds the Work,
    nor moves the total, nor is rebuilt letter by letter. The child's answer is the one for a word
    nobody has; the administrator finds it."""
    seed = world.seed
    _credit(world, "edition", seed.editions[hidden_edition], ["Private Author"])
    absent = listing(world.kid, q="zzzqqq")
    for word in ("private", "Private Author", "priv", "p", "rivate auth"):
        got = listing(world.kid, q=word)
        assert {k: v for k, v in got.items() if k != "since"} == {
            k: v for k, v in absent.items() if k != "since"
        }, word
    assert seed.works[work_key] in ids(
        world.admin, q="private"
    )  # the administrator sees every Edition


def test_q_reads_the_authors_named_on_an_edition_she_may_see(world: World) -> None:  # noqa: F811
    seed = world.seed
    _credit(world, "edition", seed.editions["ok"], ["Open Author"])
    assert ids(world.kid, q="open author") == {seed.works["ok"]}


def test_q_reads_the_decoded_names_not_the_json_text(world: World) -> None:  # noqa: F811
    seed = world.seed
    _credit(world, "work", seed.works["ok"], ["Hervé Tullet", {"name": "Zoé Dupont"}])
    assert ids(world.admin, q="hervé") == {seed.works["ok"]}
    assert ids(world.admin, q="herve") == {seed.works["ok"]}  # accents fold on names too
    assert ids(world.admin, q="ZOÉ") == {seed.works["ok"]}
    assert ids(world.admin, q="name") == set()  # the key of an object is no author
    assert ids(world.admin, q="u00e9") == set()  # nor the escape of a letter
    assert ids(world.kid, q="hervé") == {seed.works["ok"]}
