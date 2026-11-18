"""What a read marker of a Mihon backup may become, once its series is resolved (EF-INT-008).

The import writes a marker for the profile the admin chose and places no guard: a marker is not a
reading position, and nothing a child sees reads it. The guard is the resolution's. When a pending
item is resolved to an Edition, `readable_markers` says which markers may become positions on it:
those of a member (not narrowed), and those of a child only if the Edition is open to her, by the
one function that every surface asks (`visible_edition_ids`: her ceiling, and a shelf that names
her or is open to all). An Edition that does not exist answers like a hidden one: nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from shelfhold.catalog.public import visible_edition_ids
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.schema import acquisition, identity

__all__ = ["ReadableMarker", "readable_markers"]


@dataclass(frozen=True)
class ReadableMarker:
    user_id: str
    chapter_ref: str
    number: str | None
    read: bool
    last_page: int | None


def readable_markers(
    connection: sa.Connection, pending_item_id: str, edition_id: str
) -> list[ReadableMarker]:
    """The markers of the pending item that may become reading positions on this Edition, read
    now (a profile taken off a shelf loses the guard at the next call)."""
    marker, user = acquisition.read_marker.c, identity.user.c
    rows = connection.execute(
        sa.select(
            marker.user_id, marker.chapter_ref, marker.number, marker.read, marker.last_page,
            user.role,
        )
        .join(identity.user, user.id == marker.user_id)
        .where(marker.pending_item_id == pending_item_id)
        .order_by(marker.user_id, marker.chapter_ref)
    ).all()  # fmt: skip
    open_to: dict[str, bool] = {}
    kept: list[ReadableMarker] = []
    for row in rows:
        user_id = str(row.user_id)
        if row.role == "child" and user_id not in open_to:
            scope = reader_scope(connection, user_id)
            open_to[user_id] = bool(visible_edition_ids(connection, scope, [edition_id]))
        if row.role == "child" and not open_to[user_id]:
            continue
        kept.append(
            ReadableMarker(
                user_id=user_id, chapter_ref=str(row.chapter_ref),
                number=None if row.number is None else str(row.number),
                read=bool(row.read), last_page=row.last_page,
            )
        )  # fmt: skip
    return kept
