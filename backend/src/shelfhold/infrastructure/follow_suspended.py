"""The Works whose following is suspended on a shelf (EF-EXP-048).

Adopting a library the children can read leaves every Work nobody rated in "À classer" with its
following suspended: the list lives in `shelf.follow_suspended`, one entry per Work, never per
shelf, so the rest of the shelf goes on. Rating the Work lifts the suspension, in the same
transaction (`RatingService.rate`). Plain functions over the table, since the follow cycle
(acquisition), the adoption (export) and the rating (catalog) all read the same list.
"""

from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import export

__all__ = ["lift", "suspend", "suspended_work_ids"]


def _lists(connection: Connection) -> dict[str, list[str]]:
    shelf = export.shelf
    rows = connection.execute(sa.select(shelf.c.id, shelf.c.follow_suspended))
    return {r.id: [str(w) for w in (r.follow_suspended or [])] for r in rows}


def suspended_work_ids(connection: Connection) -> frozenset[str]:
    """Every Work whose following is suspended, on any shelf."""
    return frozenset(w for works in _lists(connection).values() for w in works)


def suspend(connection: Connection, shelf_id: str, work_ids: Iterable[str]) -> int:
    """Add Works to the list of the shelf; the number newly suspended."""
    shelf = export.shelf
    current = _lists(connection).get(shelf_id)
    if current is None:
        raise LookupError(f"no such shelf: {shelf_id}")
    fresh = sorted(set(work_ids) - set(current))
    if fresh:
        connection.execute(
            sa.update(shelf)
            .where(shelf.c.id == shelf_id)
            .values(follow_suspended=[*current, *fresh])
        )
    return len(fresh)


def lift(connection: Connection, work_id: str) -> int:
    """Take a Work off every list (it was rated); the number of shelves that listed it."""
    shelf = export.shelf
    lifted = 0
    for shelf_id, works in _lists(connection).items():
        if work_id in works:
            connection.execute(
                sa.update(shelf)
                .where(shelf.c.id == shelf_id)
                .values(follow_suspended=[w for w in works if w != work_id])
            )
            lifted += 1
    return lifted
