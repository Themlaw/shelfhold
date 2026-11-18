"""The ids of the sessions of a profile that are still open (M5 L05a).

What a cookieless request asks (`/content` on a separate origin): is there a session of this
profile, not revoked and not past its absolute expiry? The caller compares fingerprints; the id of
a session is not a credential (the cookie holds the token, only its hash is stored).
"""

from datetime import datetime

import sqlalchemy as sa

from shelfhold.infrastructure.schema import identity


def live_session_ids(connection: sa.Connection, user_id: str, now: datetime) -> list[str]:
    table = identity.session
    return [
        str(row)
        for row in connection.execute(
            sa.select(table.c.id).where(
                table.c.user_id == user_id,
                table.c.revoked_at.is_(None),
                table.c.absolute_expiry > now,
            )
        ).scalars()
    ]
