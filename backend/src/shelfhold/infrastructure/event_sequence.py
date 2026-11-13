"""The sequence of the `event` table, and the one way to cut every `?since=` token off it (M3 L11).

A purge of a Work leaves no trace (EF-BIB-053): the events that name it are erased, and no event
may name it afterwards, not even by its identifier. The removal cannot be told to the clients that
hold the Work, so every token issued before the purge is made unanswerable instead: the sequence
moves by one and the floor of the answerable tokens moves to it. The next call of every client is
`resync_required` (a reload of the list, which no longer holds the Work); the tokens issued after
are good.
"""

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import platform

__all__ = ["invalidate_tokens"]


def invalidate_tokens(connection: Connection) -> int:
    """Move the sequence by one, then make every token below the new head `resync_required`.
    Returns the new head. In the transaction of the purge: both or none."""
    connection.execute(
        sa.text(
            "INSERT INTO sqlite_sequence (name, seq) SELECT 'event', 0 "
            "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'event')"
        )
    )
    connection.execute(sa.text("UPDATE sqlite_sequence SET seq = seq + 1 WHERE name = 'event'"))
    head = int(
        connection.execute(
            sa.text("SELECT seq FROM sqlite_sequence WHERE name = 'event'")
        ).scalar_one()
    )
    connection.execute(
        sa.update(platform.instance).values(
            events_purged_through=sa.func.max(platform.instance.c.events_purged_through, head)
        )
    )
    return head
