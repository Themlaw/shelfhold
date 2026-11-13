"""Is the credential of an open stream still good? (EF-USR-021)

A session, a key or a device token is checked when a request starts; a stream outlives that
check, so it asks again as it goes: a session revoked or past its absolute expiry, a key revoked
or expired, a profile that is gone, all end it.
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.identity.domain.credentials import (
    Credential,
    Principal,
    SessionTimes,
    session_expired,
    session_policy,
)
from shelfhold.infrastructure.schema import identity

__all__ = ["credential_is_live"]


def credential_is_live(
    engine: Engine, principal: Principal, now: datetime, *, hardened: bool = True
) -> bool:
    """Read at the call, never cached. An unknown kind of credential is not live. A session is
    judged by the door's own rule (`session_expired` under the policy of its role: stored expiry,
    sliding idle, the absolute cap of the hardened profile), without touching `last_seen_at`: an
    open stream is not activity. `hardened` defaults to the strict reading."""
    with engine.connect() as connection:
        if (
            connection.execute(
                sa.select(identity.user.c.id).where(identity.user.c.id == principal.user_id)
            ).first()
            is None
        ):
            return False
        match principal.credential:
            case Credential.SESSION:
                row = connection.execute(
                    sa.select(
                        identity.session.c.revoked_at,
                        identity.session.c.created_at,
                        identity.session.c.last_seen_at,
                        identity.session.c.absolute_expiry,
                    ).where(identity.session.c.id == principal.session_id)
                ).first()
                return (
                    row is not None
                    and row.revoked_at is None
                    and not session_expired(
                        SessionTimes(row.created_at, row.last_seen_at, row.absolute_expiry),
                        session_policy(principal.role, hardened=hardened),
                        now,
                    )
                )
            case Credential.API_KEY | Credential.DEVICE_TOKEN:
                key = connection.execute(
                    sa.select(identity.api_key.c.revoked_at, identity.api_key.c.expires_at).where(
                        identity.api_key.c.id == principal.key_id
                    )
                ).first()
                return (
                    key is not None
                    and key.revoked_at is None
                    and (key.expires_at is None or key.expires_at > now)
                )
            case _:
                return False
