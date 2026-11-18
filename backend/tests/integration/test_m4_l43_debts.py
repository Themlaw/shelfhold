"""M4 L43: two debts of the milestone, closed.

* L39 (D-P3): deleting a shelf takes its Editions off every shelf, so a child who saw a Work
  through it stops seeing it. `?since=` tells her (one `ops.profile_visibility` event for her), and
  tells nobody who never saw it.
* L26 (EF-INT-008): a read marker of a Mihon backup becomes a reading position only on an Edition
  that is open to the child it belongs to. The guard is the resolution's, never the import's: the
  import places none. The guard has no route of its own, so the attacks are the ones the data
  allows: another child's shelf (IDOR on an Edition), a member (not narrowed), a profile taken off
  the list of a shelf (closes at the next call), an Edition that does not exist (the same answer
  as a hidden one).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.read_markers import readable_markers
from shelfhold.catalog.public import removed_ids, window
from shelfhold.headless import records
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, export, platform
from tests.integration import test_m4_l39_allowed_profiles as l39
from tests.integration.test_m4_l39_allowed_profiles import World

world = l39.world
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
ABSENT_EDITION = "01HZZZZZZZZZZZZZZZZZZZZZZM"


# -- L39: a deleted shelf is a removal for the child who saw a Work through it -------------------


@pytest.mark.req("EF-USR-006", "EF-INT-001")
def test_deleting_a_shelf_tells_the_child_who_saw_its_work(world: World) -> None:
    with world.seed.engine.connect() as connection:
        token = window(connection).head
    with world.seed.engine.begin() as connection:
        records.unbind(connection, world.shelf_id, "no-destination", drop_shelf=True)
    with world.seed.engine.connect() as connection:
        sib = reader_scope(connection, world.sib_id)
        kid = reader_scope(connection, world.kid_id)
        assert world.work_id in removed_ids(connection, sib, token)
        assert world.work_id not in removed_ids(connection, kid, token)  # she never saw it
        events = connection.execute(
            sa.select(platform.event.c.payload).where(
                platform.event.c.type == "ops.profile_visibility"
            )
        ).all()
    assert [(e.payload["profile_id"], e.payload["visible_after"]) for e in events] == [
        (world.sib_id, False)
    ]
    assert all("title" not in e.payload for e in events)


@pytest.mark.req("EF-USR-006", "EF-INT-001")
def test_deleting_a_shelf_nobody_saw_writes_nothing(world: World) -> None:
    with world.seed.engine.begin() as connection:
        empty = new_ulid()
        connection.execute(
            sa.insert(export.shelf).values(
                id=empty, name="Empty", phrase="empty", max_age_rating=10, allowed_profiles=[]
            )
        )
        before = window(connection).head
        records.unbind(connection, empty, "no-destination", drop_shelf=True)
        after = window(connection).head
    # the shelf removal itself is one event (`ops.shelf_removed`); no profile event rides on it
    assert after == before + 1


# -- L26: a read marker is a position only where the child may see the Edition -------------------


def _marker(world: World, user_id: str, item_id: str, chapter: str, *, read: bool = True) -> None:
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.read_marker).values(
                id=new_ulid(), user_id=user_id, pending_item_id=item_id, chapter_ref=chapter,
                number=chapter, read=read, created_at=NOW,
            )
        )  # fmt: skip


def _item(world: World) -> str:
    item = new_ulid()
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.pending_source_item).values(
                id=item, source_key="mihon:1", series_ref=f"ref-{item}", title="A Series",
                fetched_at=NOW,
            )
        )  # fmt: skip
    return item


def _readable(world: World, item: str, edition: str) -> set[tuple[str, str]]:
    with world.seed.engine.connect() as connection:
        return {(m.user_id, m.chapter_ref) for m in readable_markers(connection, item, edition)}


@pytest.mark.req("EF-INT-008", "ENF-SEC-010")
def test_a_child_marker_is_readable_only_on_an_edition_her_shelves_open(world: World) -> None:
    item = _item(world)
    _marker(world, world.sib_id, item, "1")
    _marker(world, world.kid_id, item, "2")
    # `world.edition_id` is on a shelf that only `sib` may see
    assert _readable(world, item, world.edition_id) == {(world.sib_id, "1")}
    # an Edition on the shelf open to everyone: both children's markers
    open_edition = world.seed.editions["adult_shelf"]
    assert _readable(world, item, open_edition) == {(world.sib_id, "1"), (world.kid_id, "2")}


@pytest.mark.req("EF-INT-008", "ENF-SEC-010")
def test_a_member_marker_is_not_narrowed(world: World) -> None:
    with world.seed.engine.connect() as connection:
        mum = world._id(connection, "mum")
    item = _item(world)
    _marker(world, mum, item, "7")
    assert _readable(world, item, world.edition_id) == {(mum, "7")}


@pytest.mark.req("EF-INT-008", "ENF-SEC-010")
def test_a_profile_taken_off_the_shelf_loses_the_guard_at_the_next_call(world: World) -> None:
    item = _item(world)
    _marker(world, world.kid_id, item, "3")
    assert _readable(world, item, world.edition_id) == set()
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(export.shelf)
            .where(export.shelf.c.id == world.shelf_id)
            .values(allowed_profiles=[world.sib_id, world.kid_id])
        )
    assert _readable(world, item, world.edition_id) == {(world.kid_id, "3")}
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(export.shelf)
            .where(export.shelf.c.id == world.shelf_id)
            .values(allowed_profiles=[world.sib_id])
        )
    assert _readable(world, item, world.edition_id) == set()


@pytest.mark.req("EF-INT-008", "ENF-SEC-010")
def test_an_unknown_edition_answers_like_a_hidden_one(world: World) -> None:
    item = _item(world)
    _marker(world, world.kid_id, item, "4")
    assert _readable(world, item, ABSENT_EDITION) == set()
    assert _readable(world, item, world.edition_id) == set()
