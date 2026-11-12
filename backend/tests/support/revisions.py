"""The whole metadata push of a revision queue on one connection, as its tests drive it.

Production composes the same two parts (`metadata_candidates`, then `settle_metadata` in a
transaction of its own per row, because the Kavita call is never awaited inside a write
transaction: `ops.revisions`); a test that holds one connection does not need that split.
"""

from typing import Protocol

import sqlalchemy as sa

from shelfhold.catalog.application.revisions import RevisionQueue


class MetadataPusher(Protocol):
    def push(self, connection: sa.Connection, *, unit_id: str, field: str, value: str) -> bool:
        """Set one field through the Kavita API; False when it cannot be (a lock that is not
        Shelfhold's, no series known there). Idempotent: a replay sets the same value."""
        ...


def apply_metadata(
    queue: RevisionQueue, pusher: MetadataPusher, connection: sa.Connection
) -> list[str]:
    """Push the revisions that change only a metadata field; a push that raises leaves its row
    open (the next run sets the same value again)."""
    applied: list[str] = []
    for item in queue.metadata_candidates():
        if item.field is None or item.value is None:
            continue
        taken = pusher.push(connection, unit_id=item.unit_id, field=item.field, value=item.value)
        queue.settle_metadata(item.id, taken=taken)
        if taken:
            applied.append(item.id)
    return applied
