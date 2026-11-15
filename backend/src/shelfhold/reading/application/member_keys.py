"""A member's key on a destination is read live before every call that uses it (EF-USR-012).

`revoked_at` is read from the database each time, never kept from an earlier call: a key revoked
between two calls of a synchronisation is refused at the second. A member has a key of her own
per destination; another member's account is never returned for her.
"""

import sqlalchemy as sa

from shelfhold.infrastructure.schema import identity

__all__ = ["MemberKeyRevokedError", "live_member_account"]


class MemberKeyRevokedError(LookupError):
    """No live key for this member on this destination (revoked, removed or never given)."""

    def __init__(self) -> None:
        super().__init__("no live member key")


def live_member_account(
    connection: sa.Connection, *, user_id: str, shelf_destination_id: str
) -> str:
    """The id of the member's live account on the destination, or `MemberKeyRevokedError`."""
    account = identity.destination_account
    row = connection.execute(
        sa.select(account.c.id).where(
            account.c.user_id == user_id,
            account.c.shelf_destination_id == shelf_destination_id,
            account.c.revoked_at.is_(None),
            account.c.secret_ct.is_not(None),
        )
    ).first()
    if row is None:
        raise MemberKeyRevokedError
    return str(row.id)
