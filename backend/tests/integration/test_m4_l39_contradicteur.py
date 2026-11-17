"""PR #333 (M4 L39, D-P3), relecture adverse : contrôle parental et vie privée.

Each test is the proof of a hole found in review. They were red in strict xfail; the holes are
closed (C1: a waiting Edition carries nothing; C2: `?since=` tells a removal that a shelf list
makes) and they are plain tests now.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import visible_edition_ids, visible_work_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.integration import test_m4_l39_allowed_profiles as l39
from tests.integration.test_m4_l39_allowed_profiles import World

pytestmark = pytest.mark.req("EF-USR-006", "EF-USR-015")

WORKS = "/api/v1/works"
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
world = l39.world  # the fixture of L39: a Work filed only on the shelf of `sib`


def _page(world: World) -> set[str]:
    answer = world.kid.get(WORKS, params={"limit": 200})
    assert answer.status_code == 200, answer.text
    return {item["work_id"] for item in answer.json()["items"]}


# -- C1: an Edition waiting in "À classer" carries the Work to a child ---------------------------


def test_poc_a_waiting_edition_on_an_open_shelf_does_not_carry_the_work(world: World) -> None:
    """The Work of the sibling's shelf gets a second Edition on the shared shelf, rated 6, but a
    provider suggests 16 for it: it waits in "À classer" (`visible_editions_clause` hides it). By
    ADR-0027 as amended, it carries nothing; the Work must stay the sibling's alone."""
    with world.seed.engine.begin() as connection:
        waiting = new_ulid()
        connection.execute(
            sa.insert(catalog.edition).values(
                id=waiting, work_id=world.work_id, lang="fr", translation_type="official",
                media="images", rated_at=NOW, shelf_id=world.seed.shelves["adult"],
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(catalog.age_suggestion).values(
                id=new_ulid(), entity_type="edition", entity_id=waiting, value=16,
                provenance="provider", seen_at=NOW, state="open",
            )
        )  # fmt: skip
        scope = reader_scope(connection, world.kid_id)
        # the control: no Edition of this Work is hers, the waiting one included
        assert visible_edition_ids(connection, scope, [waiting, world.edition_id]) == set()
        hers = visible_work_ids(connection, scope, [world.work_id])
    assert hers == set(), "a Work with no Edition she may see is shown to her"
    assert world.work_id not in _page(world)


# -- C2: `?since=` never reports a Work taken away by a shelf list ---------------------------------


def test_poc_since_reports_a_work_taken_away_by_a_shelf_list(world: World) -> None:
    work = world.seed.works["adult_shelf"]  # filed only on the shared "Adults" shelf
    first = world.kid.get(WORKS, params={"limit": 200})
    token = str(first.json()["since"])
    assert work in {i["work_id"] for i in first.json()["items"]}
    answer = world.env.post(
        f"/api/v1/shelves/{world.seed.shelves['adult']}/allowed-profiles",
        json={"profiles": [world.sib_id]},
    )
    assert answer.status_code == 200, answer.text
    assert work not in _page(world)  # the control: the page has closed at once
    delta = world.kid.get(WORKS, params={"since": token, "limit": 200})
    assert delta.status_code == 200, delta.text
    assert work in delta.json()["removed"], "a synced client is never told to drop it"
