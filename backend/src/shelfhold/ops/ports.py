"""What the operating surface needs from the other lots, as ports (EF-INT-023).

A component that is not connected in an instance (the restore guard of L35 before it is merged,
a Kavita-less instance) is simply `None`: its exit answers `ops.exit_unavailable` and its states
are not listed. Nothing here imports another lot's module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from shelfhold.identity.public import Principal


@dataclass(frozen=True)
class WaitingItem:
    """One thing that waits for a human, with the command that leaves the wait."""

    state: str  # a `WaitingState` value (or the failure class of a paused service)
    queue: str  # the paused queue it belongs to (`follow`, `export`, `jobs.<kind>`...)
    subject: str  # the id the command is given
    reason: str
    command: str = ""  # filled by the service when the port leaves it empty
    reserved: bool = False


@dataclass(frozen=True)
class PausedQueue:
    queue: str
    reason: str
    command: str = ""


class TargetOps(Protocol):
    """Kavita: look again (`recheck`) and take the pauses off (`reconnect`)."""

    def recheck(self, target_id: str) -> None: ...
    def reconnect(self, target_id: str) -> None: ...


class HeadlessOps(Protocol):
    """The credential renewals of the configuration facade (`HeadlessAdmin`)."""

    def target_rotate_key(self, *, target: str, password: str) -> object: ...
    def target_find_key(self, *, target: str, password: str | None) -> tuple[str, str] | None: ...
    def target_revoke_key_id(
        self, *, target: str, password: str | None, key_id: str, key_digest: str | None = None
    ) -> str: ...
    def source_set_secret(self, secret: str) -> object: ...
    def source_recheck(self) -> object: ...
    def source_reconnect(self) -> object: ...


class RestoreExit(Protocol):
    """L35: the instance restored behind its disk is read only until it is adopted again."""

    def waiting(self) -> list[WaitingItem]: ...
    def adopt_after_restore(self, *, confirm_deletion: bool, principal: Principal) -> None: ...
    def forget_root(self, *, root_id: str, confirm: str, principal: Principal) -> None: ...


class CredentialsExit(Protocol):
    """After a restore: sessions and API keys were revoked; the passkeys and the passwords changed
    since the backup are for a person to look at. `acknowledge` ends the wait."""

    def waiting(self) -> list[WaitingItem]: ...
    def acknowledge(self, *, principal: Principal) -> list[str]: ...


class KeyringExit(Protocol):
    """L33: tombstone lines whose key the keyring lacks."""

    def waiting(self) -> list[WaitingItem]: ...
    def accept_lost_keyring(
        self, *, confirmation: str, principal: Principal, frozen: bool = False
    ) -> list[str]: ...
    def set_aside_journal(
        self, *, principal: Principal, root_id: str | None = None
    ) -> dict[str, str]: ...
    def flush_owed(self) -> list[str]: ...
    def rekey(self, *, principal: Principal, kids: Sequence[str]) -> dict[str, Any]: ...
    def import_keyring(
        self,
        *,
        passphrase_file: str,
        in_file: str,
        principal: Principal,
        replay: bool = True,
        confined: bool = False,
    ) -> dict[str, Any]: ...


class AdoptionExit(Protocol):
    """L29 / L30: adopted series that wait for a decision."""

    def waiting(self) -> list[WaitingItem]: ...
    def set_follow_from(self, *, destination_id: str, series: str, chapter: str) -> None: ...
    def confirm_migration(self, *, destination_id: str, fingerprint: str) -> None: ...
