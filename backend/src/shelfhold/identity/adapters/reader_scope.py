"""The scope of the profile that reads, read from the database at each request (EF-USR-006).

The ceiling and the role are never kept in a session or a key: a ceiling lowered, a role changed,
a profile removed, take effect on the next request. A key or a token reads as its own profile (the
reader), never as whoever issued it. A profile that is not there, or a role the rules do not know,
sees nothing.
"""

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.identity.domain.visibility import NOBODY, ViewerScope, scope_of
from shelfhold.infrastructure.schema import identity

__all__ = ["reader_scope"]


def reader_scope(source: Engine | Connection, user_id: str | None) -> ViewerScope:
    if not isinstance(user_id, str) or not user_id:
        return NOBODY
    query = sa.select(identity.user.c.role, identity.user.c.max_age_rating).where(
        identity.user.c.id == user_id
    )
    if isinstance(source, Engine):
        with source.connect() as connection:
            row = connection.execute(query).first()
    else:
        row = source.execute(query).first()
    return NOBODY if row is None else scope_of(row.role, row.max_age_rating)
