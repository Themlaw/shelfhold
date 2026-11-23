"""M6 L17a, contre-relecture: a full sync started over a device that already holds books must
still take back what the profile may no longer see (EF-INT-012, EF-USR-006).

The snapshot fix announces as deleted only what the point of a *delta* held. A full enumeration
freezes `gone = []` and writes the point from what it served alone: every book the device held
from an earlier sync, and that the child may no longer see, leaves the point without ever being
deleted — the device keeps it for good. A full sync starts on any token that is not the current
one: none, garbage, or the page cursor of a flight another request replaced.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog
from shelfhold.reading.application.kobo_sync import Cursor, _encode, entitlement_of
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


def _hide_adult_shelf_book(world: World) -> str:
    """The child holds it; it is filed on a shelf closed to her: hidden from now on."""
    world.write(
        sa.update(catalog.edition)
        .where(catalog.edition.c.id == world.seed.editions["adult_shelf"])
        .values(shelf_id=world.seed.shelves["family"])
    )
    return entitlement_of(world.seed.editions["adult_shelf"])


def _deleted(pages: list[object]) -> set[str]:
    return {e for page in pages for e in result_of(page)["DeletedEntitlements"]}


@pytest.mark.parametrize("token", [None, "garbage"])
def test_a_full_sync_over_a_held_book_takes_it_back(world: World, token: str | None) -> None:
    kid = world.kid
    pages, _done = sync_all(kid)
    hidden = entitlement_of(world.seed.editions["adult_shelf"])
    assert hidden in books_of(pages)  # the device holds it
    _hide_adult_shelf_book(world)

    # the device starts over (no token, or one that is not ours): a full enumeration
    restarted, done_again = sync_all(kid, token)
    then, _ = sync_all(kid, done_again)
    assert hidden in _deleted(restarted) | _deleted(then), "still on the child's device"


def test_a_stale_page_cursor_restarts_without_forgetting_a_held_book(world: World) -> None:
    kid = world.kid
    _pages, done = sync_all(kid)
    hidden = _hide_adult_shelf_book(world)
    for index in range(30):  # a delta of more than one page
        add_work(world, f"small{index}", age=8, shelf="adult")
    first = kid.get(SYNC, headers={"x-kobo-synctoken": done})
    assert first.headers.get("x-kobo-sync") == "continue"
    assert result_of(first)["DeletedEntitlements"] == []  # said at the last page of the flight

    # the next page arrives with a cursor of a flight that is no longer the one in place
    # (another request of the device replaced it): the service starts a full sync over
    page_two = first.json()["ContinuationToken"]
    stale = _encode(Cursor(world.seed.editions["small0"], 1))
    assert stale != page_two
    restarted, done_again = sync_all(kid, stale)
    then, _ = sync_all(kid, done_again)
    assert hidden in _deleted(restarted) | _deleted(then), "still on the child's device"
