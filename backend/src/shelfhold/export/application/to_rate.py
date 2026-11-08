"""The Works that wait in "À classer" (EF-EXP-048, EF-BIB-055, EF-INT-023).

Two reasons put an unrated Work there, and each is a waiting state with `rate` as its exit:

- `follow_suspended`: it came with the adoption of a library the children can read; following is
  suspended for it alone, the rest of the shelf goes on (`shelf.follow_suspended`);
- `rating_pending`: a shelf below 18+ exists and an Edition of the Work nobody rated waits out of
  it (held until rated, withdrawn, or new since the last rating).

`status`, the batch of `rate` and the screen of L12 all read this one list.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.follow_suspended import suspended_work_ids
from shelfhold.infrastructure.schema import catalog, export

__all__ = ["FOLLOW_SUSPENDED", "RATING_PENDING", "ToRate", "batch_token", "to_rate"]

FOLLOW_SUSPENDED = "follow_suspended"
RATING_PENDING = "rating_pending"


@dataclass(frozen=True)
class ToRate:
    work_id: str
    title: str
    state: str  # FOLLOW_SUSPENDED | RATING_PENDING
    reason: str


def to_rate(connection: Connection) -> list[ToRate]:
    """Every unrated Work that waits, in the order of its id; a suspended one is listed once,
    as `follow_suspended`."""
    work, edition, shelf = catalog.work, catalog.edition, export.shelf
    suspended = suspended_work_ids(connection)
    titles = (
        {
            r.id: (r.title, r.rated_at is not None)
            for r in connection.execute(sa.select(work.c.id, work.c.title, work.c.rated_at))
        }
        if suspended
        else {}
    )
    found = [
        ToRate(
            w, titles[w][0], FOLLOW_SUSPENDED,
            "adopted from a library children can read: following suspended until it is rated",
        )
        for w in sorted(suspended)
        if w in titles and not titles[w][1]
    ]  # fmt: skip
    family = (
        connection.execute(sa.select(shelf.c.id).where(shelf.c.max_age_rating < 18)).scalars().all()
    )
    if not family:
        return found
    rows = connection.execute(
        sa.select(work.c.id, work.c.title)
        .distinct()
        .select_from(work.join(edition, edition.c.work_id == work.c.id))
        .where(
            edition.c.rated_at.is_(None),
            sa.or_(edition.c.shelf_id.is_(None), edition.c.shelf_id.in_(family)),
        )
        .order_by(work.c.id)
    )
    found += [
        ToRate(
            r.id, r.title, RATING_PENDING,
            "an Edition nobody rated waits out of the shelves below 18+",
        )
        for r in rows
        if r.id not in suspended
    ]  # fmt: skip
    return found


def batch_token(items: Sequence[ToRate], age: str) -> str:
    """What the admin types to rate them all: the count and a digest of which Works at which age,
    so a list that moved since it was read is not confirmed by an old token."""
    digest = hashlib.sha256("\n".join([age, *sorted(i.work_id for i in items)]).encode())
    return f"{len(items)}-{digest.hexdigest()[:8]}"
