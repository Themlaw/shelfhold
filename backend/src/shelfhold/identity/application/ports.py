"""What the local authentication needs from storage and from a password hasher."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class UserRecord:
    id: str
    username: str
    role: str
    password_hash: str | None
    can_rate: bool = False


@dataclass(frozen=True)
class SessionRecord:
    id: str
    user: UserRecord
    csrf_secret: str
    created_at: datetime
    last_seen_at: datetime
    auth_time: datetime
    absolute_expiry: datetime
    remember: bool
    revoked_at: datetime | None


@dataclass(frozen=True)
class NewSession:
    id: str
    user_id: str
    token_hash: str
    csrf_secret: str
    now: datetime
    absolute_expiry: datetime
    remember: bool
    ip: str | None
    passkey_id: str | None = None  # the passkey that opened it: revoking that passkey closes it


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    user: UserRecord
    name: str | None
    prefix: str
    hash: str
    scopes: tuple[str, ...]
    read_only: bool
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class NewApiKey:
    id: str
    user_id: str
    name: str | None
    prefix: str
    hash: str
    scopes: tuple[str, ...]
    read_only: bool
    now: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class AuditEntry:
    actor_id: str | None
    action: str
    at: datetime
    target: str | None = None
    session_id: str | None = None
    auth_time: datetime | None = None


@dataclass(frozen=True)
class PasskeyRecord:
    id: str
    user_id: str
    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    aaguid: str | None
    name: str
    created_at: datetime
    rp_id: str | None
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class NewPasskey:
    id: str
    user_id: str
    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    aaguid: str | None
    name: str
    now: datetime
    rp_id: str | None = None


class PasskeyGoneError(Exception):
    """The passkey behind a session being opened was revoked meanwhile: nothing was written."""


class KeyConflictError(Exception):
    """The new key's prefix is taken: nothing was written."""


class IdentityStore(Protocol):
    def user_by_name(self, username: str) -> UserRecord | None: ...
    def user_by_id(self, user_id: str) -> UserRecord | None: ...
    def admins(self) -> Sequence[UserRecord]: ...
    def has_role(self, role: str) -> bool: ...
    def restore_notice_active(self) -> bool: ...
    def insert_user(
        self, *, id: str, username: str, role: str, password_hash: str, now: datetime
    ) -> None: ...
    def set_password_hash(self, user_id: str, password_hash: str) -> None: ...
    def delete_user(self, user_id: str) -> str | None: ...

    def insert_session(self, session: NewSession) -> None:
        """`PasskeyGoneError` when `session.passkey_id` is revoked at the moment of writing: the
        check and the insert are one transaction, so a revocation cannot slip between them."""
        ...

    def session_by_token_hash(self, token_hash: str) -> SessionRecord | None: ...
    def touch_session(
        self, session_id: str, *, now: datetime, absolute_expiry: datetime
    ) -> None: ...
    def rotate_session(
        self, session_id: str, *, token_hash: str, csrf_secret: str, now: datetime
    ) -> None: ...
    def revoke_session_by_token_hash(self, token_hash: str, *, now: datetime) -> None: ...
    def revoke_sessions(
        self, *, now: datetime, user_id: str | None = None, session_id: str | None = None
    ) -> int: ...

    def insert_api_key(self, key: NewApiKey) -> None: ...
    def api_key_by_prefix(self, prefix: str) -> ApiKeyRecord | None: ...
    def api_keys(self) -> Sequence[ApiKeyRecord]: ...
    def touch_api_key(self, key_id: str, *, now: datetime) -> None: ...
    def revoke_api_key(self, prefix: str, *, now: datetime) -> bool: ...
    def rotate_api_key(self, old_prefix: str, new: NewApiKey, *, audit: AuditEntry) -> bool:
        """Revoke the live key `old_prefix`, store `new` and the audit line, all or nothing.
        False: no live key has that prefix (nothing written). `KeyConflictError`: prefix taken."""
        ...

    def insert_passkey(self, passkey: NewPasskey) -> bool:
        """False: that credential id is already stored (by anyone, revoked or not)."""
        ...

    def passkeys_of(self, user_id: str) -> Sequence[PasskeyRecord]:
        """The live passkeys, oldest first."""
        ...

    def passkey_by_credential(self, credential_id: bytes) -> PasskeyRecord | None:
        """Revoked ones too: the caller refuses them."""
        ...

    def record_passkey_use(self, passkey_id: str, *, sign_count: int, now: datetime) -> bool:
        """Atomic: False when the stored counter already reached `sign_count` (a replay lost the
        race), or when the passkey was revoked meanwhile. A counter that never counts stays 0."""
        ...

    def revoke_passkey(self, user_id: str, passkey_id: str, *, now: datetime) -> str:
        """`revoked`, `not_found`, or `last_factor` (no password and no other live passkey);
        decided and written in one transaction, with the sessions that passkey opened."""
        ...

    def passkey_rp_ids(self) -> set[str]:
        """The RP IDs of the live passkeys that know theirs."""
        ...

    def reset_credentials(
        self, user_id: str, password_hash: str, *, now: datetime
    ) -> tuple[int, int]:
        """New password, every session closed, every passkey revoked: one transaction. Returns
        (sessions closed, passkeys revoked)."""
        ...

    def audit(self, entry: AuditEntry) -> None: ...
    def audit_targets(self, action: str, *, since: datetime) -> list[str]: ...


class PasswordHasherPort(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, digest: str | None, password: str) -> bool:
        """False on mismatch; with no digest, still spends the time of a verification."""
        ...

    def needs_rehash(self, digest: str) -> bool: ...


class ReauthMethod(Protocol):
    """One way to prove presence again (`password` now, `passkey` in M1b, EF-USR-025)."""

    name: str

    def verify(self, user: UserRecord, payload: Mapping[str, object]) -> bool: ...


class DeviceProofPort(Protocol):
    """Signed proof that a browser already logged in to an account (skips the account counter)."""

    def issue(self, username: str, now: datetime) -> str: ...

    def verify(self, value: str | None, username: str, now: datetime) -> bool: ...
