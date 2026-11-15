"""SQL storage of the members' Kavita accounts: `destination_account`, the key sealed (EF-USR-012).

The key is sealed by the `SecretVault` to its row (table, column and row id are part of what is
authenticated), the fingerprint is a keyed hash that says "this key" without revealing it, and the
audit line is written in the transaction that changes the row. Revoking erases the sealed columns
and frees the account name: a live row always holds a key (a constraint of the table).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.identity.adapters.store import audit_insert, erase_accounts
from shelfhold.identity.application.destination_accounts import (
    AccountView,
    DestinationAccountError,
    UserFacts,
)
from shelfhold.identity.application.ports import AuditEntry
from shelfhold.identity.domain.kavita_mapping import same_kavita_username
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity
from shelfhold.infrastructure.secrets import SecretVault, registry

TABLE: Final = "destination_account"
_FINGERPRINT_INFO: Final = b"shelfhold/destination-account-fingerprint"
_account = identity.destination_account
_user = identity.user


def _view(row: Any) -> AccountView:
    revoked = row.revoked_at is not None
    return AccountView(
        id=row.id,
        user_id=row.user_id,
        shelf_destination_id=row.shelf_destination_id,
        remote_user=None if revoked else row.remote_user,
        token_fingerprint=row.token_fingerprint,
        verified_at=row.verified_at,
        revoked_at=row.revoked_at,
    )


class DestinationAccountStore:
    def __init__(self, engine: Engine, vault: SecretVault) -> None:
        self._engine = engine
        self._vault = vault

    def __repr__(self) -> str:
        return "DestinationAccountStore()"

    def user(self, user_id: str) -> UserFacts | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(_user.c.id, _user.c.role, _user.c.kavita_username).where(
                    _user.c.id == user_id
                )
            ).first()
        return None if row is None else UserFacts(row.id, row.role, row.kavita_username)

    def accounts_of(self, user_id: str) -> list[AccountView]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(_account)
                .where(_account.c.user_id == user_id)
                .order_by(_account.c.created_at, _account.c.id)
            )
            return [_view(row) for row in rows]

    def _fingerprint(self, row_id: str, token: str) -> str:
        subkey = hmac.new(self._vault.keyring.active.material, _FINGERPRINT_INFO, hashlib.sha256)
        digest = hmac.new(subkey.digest(), f"{row_id}\x00{token}".encode(), hashlib.sha256)
        return digest.hexdigest()[:8]

    def save(
        self,
        user_id: str,
        destination_id: str,
        remote_user: str,
        token: str,
        *,
        declared: str | None,
        now: datetime,
        entry: Callable[[bool], AuditEntry],
    ) -> bool:
        with write_transaction(self._engine) as connection:
            current = connection.execute(
                sa.select(_user.c.role, _user.c.kavita_username).where(_user.c.id == user_id)
            ).first()
            if current is None:
                raise DestinationAccountError("not_found")
            if declared is not None and (
                current.role != "child"
                or current.kavita_username is None
                or not same_kavita_username(current.kavita_username, declared)
            ):  # the parent changed the declared account while the key was being checked
                raise DestinationAccountError("child_account_not_declared")
            others = connection.execute(
                sa.select(_account.c.user_id, _account.c.remote_user).where(
                    _account.c.shelf_destination_id == destination_id,
                    _account.c.revoked_at.is_(None),
                )
            ).all()
            if any(o.user_id != user_id and same_kavita_username(o.remote_user, remote_user)
                   for o in others):  # fmt: skip
                raise DestinationAccountError("remote_user_taken")
            existing = connection.execute(
                sa.select(_account.c.id, _account.c.revoked_at).where(
                    _account.c.user_id == user_id,
                    _account.c.shelf_destination_id == destination_id,
                )
            ).first()
            row_id = existing.id if existing is not None else new_ulid()
            replaced = existing is not None and existing.revoked_at is None
            registry.register_active_key(connection, self._vault.keyring)
            envelope = self._vault.seal(
                token.encode(), table=TABLE, column=registry.SEALED_COLUMN, row_id=row_id
            )
            values: dict[str, Any] = {
                "remote_user": remote_user,
                "token_fingerprint": self._fingerprint(row_id, token),
                "verified_at": now,
                "revoked_at": None,
                **envelope.to_columns(),
            }
            if existing is None:
                connection.execute(
                    sa.insert(_account).values(
                        id=row_id, user_id=user_id, shelf_destination_id=destination_id, **values
                    )
                )
            else:
                connection.execute(
                    sa.update(_account).where(_account.c.id == row_id).values(**values)
                )
            connection.execute(audit_insert(entry(replaced)))
        return replaced

    def revoke(
        self,
        user_id: str,
        destination_id: str,
        *,
        now: datetime,
        entry: Callable[[], AuditEntry],
    ) -> bool:
        with write_transaction(self._engine) as connection:
            return (
                erase_accounts(
                    connection, user_id, now=now, entry=lambda _d: entry(),
                    destination_id=destination_id,
                )
                > 0
            )  # fmt: skip

    def open(self, user_id: str, destination_id: str) -> str | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(_account.c.id).where(
                    _account.c.user_id == user_id,
                    _account.c.shelf_destination_id == destination_id,
                    _account.c.revoked_at.is_(None),
                )
            ).first()
            if row is None:
                return None
            return self._vault.open_row(connection, TABLE, row.id).decode()
