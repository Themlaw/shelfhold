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


class IdentityStore(Protocol):
    def user_by_name(self, username: str) -> UserRecord | None: ...
    def user_by_id(self, user_id: str) -> UserRecord | None: ...
    def admins(self) -> Sequence[UserRecord]: ...
    def has_role(self, role: str) -> bool: ...
    def insert_user(
        self, *, id: str, username: str, role: str, password_hash: str, now: datetime
    ) -> None: ...
    def set_password_hash(self, user_id: str, password_hash: str) -> None: ...

    def insert_session(self, session: NewSession) -> None: ...
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

    def audit(self, entry: AuditEntry) -> None: ...


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
