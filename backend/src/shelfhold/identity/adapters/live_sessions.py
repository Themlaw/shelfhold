"""The ids of the sessions of a profile that are still open (M5 L05a).

What a cookieless request asks (`/content` on a separate origin): is there a session of this
profile that the door itself would still open? The rule is the door's own (`session_expired` under
the policy of the role: stored expiry, idle limit, the absolute cap of the hardened profile), not a
copy of it, and a revoked session is never live. The caller compares fingerprints; the id of a
session is not a credential (the cookie holds the token, only its hash is stored).
"""

from datetime import datetime

import sqlalchemy as sa

from shelfhold.identity.domain.credentials import SessionTimes, session_expired, session_policy
from shelfhold.infrastructure.schema import identity


def live_session_ids(
    connection: sa.Connection, user_id: str, now: datetime, *, hardened: bool = True
) -> list[str]:
    table, user = identity.session, identity.user
    rows = connection.execute(
        sa.select(
            table.c.id, table.c.created_at, table.c.last_seen_at, table.c.absolute_expiry,
            user.c.role,
        )
        .join(user, user.c.id == table.c.user_id)
        .where(table.c.user_id == user_id, table.c.revoked_at.is_(None))
    )  # fmt: skip
    return [
        str(row.id)
        for row in rows
        if not session_expired(
            SessionTimes(row.created_at, row.last_seen_at, row.absolute_expiry),
            session_policy(row.role, hardened=hardened),
            now,
        )
    ]
