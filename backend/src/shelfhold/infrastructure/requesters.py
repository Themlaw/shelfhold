"""A ticket never names a user that is gone (M3 L08, EF-USR-013).

`requester_user_id` of a download or a follow request has no foreign key, so a request that was
authenticated before the deletion of its user and written after it would keep the id for ever
(`_unname` already ran). The writers call this inside the transaction that writes the ticket: it
reads the user under the same write lock the deletion takes, and answers `None` (a ticket for
nobody, its visibility unchanged) when the user is no longer there.

It is not a trigger on purpose: a trigger that names the `user` table makes the rebuild of that
table (a batch migration) fail at the rename.
"""

import re
import secrets
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import identity


def still_a_user(connection: Connection, user_id: str | None) -> str | None:
    if user_id is None:
        return None
    found = connection.execute(sa.select(identity.user.c.id).where(identity.user.c.id == user_id))
    return user_id if found.first() is not None else None


_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")


def live_actor(actor_id: str | None) -> Any:
    """The `actor_id` of an audit line, as a value for an INSERT (M3 L08, EF-USR-013).

    A line that names a user who has been deleted since the caller read her (a session cut in the
    middle of an action) must not put the id back after `_unname` ran. For an id of a person the
    value is decided by the INSERT itself, in the transaction that writes, under the write lock the
    deletion takes: the id when the user is still there, else a fresh opaque `deleted:<hex>`.
    The token of the deletion cannot be found again, on purpose (nothing links a token to an id,
    or the pseudonymisation would be reversible): the line is kept, unlinked from the others.
    A label (`session:<id>`, `local_cli:<uid>`, `system`) or no actor is not a person: unchanged."""
    if actor_id is None or not _ULID.fullmatch(actor_id):
        return actor_id
    return sa.case(
        (sa.exists().where(identity.user.c.id == actor_id), actor_id),
        else_=f"deleted:{secrets.token_hex(8)}",
    )
