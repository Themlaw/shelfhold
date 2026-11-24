"""M6 L17a, relecture adverse: the sync point must hold what the device was **given**, not what the
last page could see (EF-INT-012, EF-USR-006).

Two PoC, red on the head of the lot:

1. a book served to a child on the first page of a sync, then hidden (ceiling lowered) before the
   last page, is written out of the sync point: the next delta, on the completion token, never
   deletes it and the device keeps it — title, cover link and all;
2. a book that becomes visible between two pages, with an identifier below the cursor, is written
   into the sync point without ever being served: no later delta announces it.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog, identity
from shelfhold.reading.application.kobo_sync import entitlement_of
from tests.integration.test_l17a_kobo_sync import (
    SYNC,
    World,
    add_work,
    books_of,
    result_of,
    sync_all,
    world,
)

pytestmark = pytest.mark.req("EF-INT-012", "EF-USR-006")

__all__ = ["world"]  # the fixture of the lot's own tests


def test_a_book_hidden_mid_sync_is_deleted_by_the_next_delta(world: World) -> None:
    kid = world.kid
    for index in range(30):  # under 10, above 7: more than one page
        add_work(world, f"small{index}", age=8, shelf="adult")
    first = kid.get(SYNC)
    assert first.headers.get("x-kobo-sync") == "continue"
    given = set(books_of([first]))  # what the device now holds
    world.write(
        sa.update(identity.user).where(identity.user.c.username == "kid").values(max_age_rating=7)
    )
    _rest, done = sync_all(kid, first.headers["x-kobo-synctoken"])
    hidden_now = {
        entitlement_of(world.seed.editions[f"small{index}"]) for index in range(30)
    } & given
    assert hidden_now  # the first page did carry books she may no longer see

    # the device follows its completion token: every book it holds and may not see is deleted
    pages, _ = sync_all(kid, done)
    deleted = {e for page in pages for e in result_of(page)["DeletedEntitlements"]}
    assert hidden_now <= deleted, f"still on the child's device: {sorted(hidden_now - deleted)}"


def test_a_book_shown_mid_sync_below_the_cursor_is_announced(world: World) -> None:
    kid = world.kid
    # on the family shelf, closed to her, and older (a smaller identifier) than the page cursor
    late = add_work(world, "late", age=6, shelf="family")
    for index in range(30):
        add_work(world, f"small{index}", age=8, shelf="adult")
    first = kid.get(SYNC)
    assert first.headers.get("x-kobo-sync") == "continue"
    assert entitlement_of(late) not in books_of([first])
    # between two pages the book is filed on a shelf open to her
    world.write(
        sa.update(catalog.edition)
        .where(catalog.edition.c.id == late)
        .values(shelf_id=world.seed.shelves["adult"])
    )
    rest, done = sync_all(kid, first.headers["x-kobo-synctoken"])
    after, _ = sync_all(kid, done)
    announced = set(books_of([first, *rest, *after]))
    assert entitlement_of(late) in announced, "the sync point holds a book never served"
