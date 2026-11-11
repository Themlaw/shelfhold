"""The `restored_behind` exit over the restore guard of L35 (EF-INT-026).

`waiting` is the guard's read-only assessment of the mounted roots against their sentinels. The
adoption itself is not run from here: it needs the instance lock (the server stopped) and a
preview confirmed by its fingerprint, which only `shelfhold reconcile --adopt-after-restore`
offers. A call through the server therefore says so instead of acting.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application.restore import RestoreGuard
from shelfhold.identity.public import AuditEntry, Principal, audit_insert
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.restore_notice import clear_notice, read_notice
from shelfhold.ops.errors import ExitUnavailableError, NotWaitingError
from shelfhold.ops.ports import WaitingItem

__all__ = ["RestoreCredentialsExit", "RestoreGuardExit"]


class RestoreGuardExit:
    """`fsops` is read at each look: the roots of the library change while the server runs
    (`target add`), and a guard opened at start-up would not see the new one."""

    def __init__(
        self, engine: Engine, fsops: Callable[[], Fsops | None], clock: Clock, bus: EventBus
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._bus = bus

    def waiting(self) -> list[WaitingItem]:
        fsops = self._fsops()
        if fsops is None:
            return []
        status = RestoreGuard(self._engine, fsops, clock=self._clock, bus=self._bus).assess()
        if not status.behind:
            return []
        return [WaitingItem("restored_behind", "instance", "instance", "database older than disk")]

    def adopt_after_restore(self, *, confirm_deletion: bool, principal: Principal) -> None:
        raise ExitUnavailableError(
            "adoption needs the instance lock and a confirmed preview: stop Shelfhold and run "
            "`shelfhold reconcile --adopt-after-restore`"
        )


class RestoreCredentialsExit:
    """`restore acknowledge`: the person has looked at the passkeys and the passwords changed
    since the backup. The notice is a row of the database (written in the transaction of the
    revocation, so it outlasts every restart and every crash); until it is acknowledged, `status`
    lists it and no passkey signs in. The acknowledgement is audited, in one transaction."""

    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def waiting(self) -> list[WaitingItem]:
        with self._engine.connect() as connection:
            data = read_notice(connection)
        if data is None:
            return []
        since = data.backup_created_at or "the backup"
        reason = (
            f"{data.sessions} session(s) and {data.api_keys} API key(s) were revoked by a "
            f"restore; passkeys sign-in is suspended. Check the passkeys created or revoked "
            f"since {since}"
        )
        if data.passwords_carried:
            reason += " (revocations and password hashes of the live database carried over)"
        else:
            reason += "; passwords changed since then are back to their old value"
        if data.deleted_accounts:
            reason += (
                "; accounts deleted since then came back without password or passkeys: "
                + ", ".join(data.deleted_accounts)
            )
        if not data.passwords_carried:
            reason += (
                ". Password sign-in is suspended: on the host, `shelfhold admin reset-password`"
            )
            reason += " for each account, then `shelfhold restore acknowledge`"
        return [WaitingItem("restored_credentials", "instance", "instance", reason)]

    def acknowledge(self, *, principal: Principal) -> list[str]:
        """The passkeys that changed since the backup, as far as they are known."""
        with self._engine.begin() as connection:
            data = read_notice(connection)
            if data is None:
                raise NotWaitingError("no restore is waiting for an acknowledgement")
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id,
                        action="restore.credentials_acknowledged",
                        target="instance",
                        at=self._clock.now(),
                        session_id=principal.session_id,
                        auth_time=principal.auth_time,
                    )
                )
            )
            clear_notice(connection)
        return list(data.passkey_changes)
