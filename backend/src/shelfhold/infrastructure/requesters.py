"""A ticket never names a user that is gone (M3 L08, EF-USR-013).

`requester_user_id` of a download or a follow request has no foreign key, so a request that was
authenticated before the deletion of its user and written after it would keep the id for ever
(`_unname` already ran). The writers call this inside the transaction that writes the ticket: it
reads the user under the same write lock the deletion takes, and answers `None` (a ticket for
nobody, its visibility unchanged) when the user is no longer there.

It is not a trigger on purpose: a trigger that names the `user` table makes the rebuild of that
table (a batch migration) fail at the rename.
"""

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import identity


def still_a_user(connection: Connection, user_id: str | None) -> str | None:
    if user_id is None:
        return None
    found = connection.execute(sa.select(identity.user.c.id).where(identity.user.c.id == user_id))
    return user_id if found.first() is not None else None
