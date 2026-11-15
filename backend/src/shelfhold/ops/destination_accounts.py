"""The members' Kavita accounts, put together (EF-USR-012): identity's rules, the sealed store, and
the Kavita adapter that checks a key opens the account the member declared.

Identity decides who may do what and keeps the sealed key; the Kavita adapter asks the server who
the key is, through the guarded fetcher of the destination's target. This module is the only place
that knows both, so neither context imports the other.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine

from shelfhold.export.targets.kavita.accounts import (
    AccountCheckFailed,
    DestinationInfo,
    KavitaMemberVerifier,
)
from shelfhold.identity.public import (
    AccountRefused,
    AuditEntry,
    DestinationAccounts,
    DestinationAccountStore,
    DestinationRef,
    audit_actor,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.secrets import SecretVault

__all__ = ["open_destination_accounts"]


class _Verifier:
    """The Kavita adapter, seen as identity's port: its refusals become `AccountRefused`."""

    def __init__(self, kavita: KavitaMemberVerifier) -> None:
        self._kavita = kavita

    def destination(self, destination_id: str) -> DestinationRef | None:
        found: DestinationInfo | None = self._kavita.destination(destination_id)
        return None if found is None else DestinationRef(found.id, found.is_kavita)

    def verify(self, destination_id: str, remote_user: str, token: str) -> None:
        try:
            self._kavita.verify(destination_id, remote_user, token)
        except AccountCheckFailed as failed:
            raise AccountRefused(failed.code) from None


def open_destination_accounts(
    engine: Engine,
    vault_of: Callable[[], SecretVault],
    *,
    clock: Clock,
    fetcher_for: Callable[[str], Any],
) -> DestinationAccounts:
    """`vault_of()` opens the vault on first use. `fetcher_for(url)` is the guarded fetcher of a
    target (declared address, SSRF guard)."""

    def entry(action: str, principal: Any, target: str | None) -> AuditEntry:
        return AuditEntry(
            actor_id=audit_actor(principal) if principal is not None else None,
            action=action,
            at=clock.now(),
            target=target,
            session_id=getattr(principal, "session_id", None),
            auth_time=getattr(principal, "auth_time", None),
        )

    return DestinationAccounts(
        DestinationAccountStore(engine, vault_of),
        _Verifier(KavitaMemberVerifier(engine, fetcher_for)),
        now=clock.now,
        audit_entry=entry,
    )
