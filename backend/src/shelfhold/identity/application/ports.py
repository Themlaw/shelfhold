"""What the local authentication needs from storage and from a password hasher."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from shelfhold.i18n import DEFAULT_LANGUAGE
from shelfhold.identity.application.sessions import SessionView


@dataclass(frozen=True)
class UserRecord:
    id: str
    username: str
    role: str
    password_hash: str | None
    can_rate: bool = False
    locale: str = DEFAULT_LANGUAGE


@dataclass(frozen=True)
class ProfileRecord:
    """A household profile as the management sees it: never a password hash."""

    id: str
    username: str
    role: str
    max_age_rating: int | None
    can_rate: bool
    visibility: str | None = None  # the stored choice; `effective_visibility` says what holds
    hide_unrated_remote: bool = True  # EF-USR-018: the child's family search hides the unrated
    subject_filter: tuple[str, ...] = ()  # the words the profile added (EF-USR-018)


@dataclass(frozen=True)
class ChildKavitaAccount:
    """What the audit of the Kavita accounts needs of a child (EF-USR-017): the profile, the age
    ceiling, and the name of the account the admin declared, if any. No title, no password."""

    profile_id: str
    max_age: int
    kavita_username: str | None


@dataclass(frozen=True)
class SourcePolicyRecord:
    source_key: str
    policy: str  # the stored choice
    effective: str  # what holds now (a child on a source that is not family: forbidden)


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
    password_hash: str | None = None  # the hash the sign-in checked: re-read under the lock
    device: str | None = None  # the coarse label of the device (`device_label`), never the header


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
    subject: UserRecord | None = None  # who the key acts as; None: its owner (EF-USR-024 (4))
    issued_by: str | None = None


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
    subject_user_id: str | None = None
    issued_by: str | None = None


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


class ProofWithdrawnError(Exception):
    """The proof a session was being opened on (a password, a passkey) was withdrawn meanwhile:
    nothing was written."""


class PasskeyGoneError(ProofWithdrawnError):
    """The passkey behind a session being opened was revoked meanwhile: nothing was written."""


class KeyConflictError(Exception):
    """The new key's prefix is taken: nothing was written."""


class IdentityStore(Protocol):
    def user_by_name(self, username: str) -> UserRecord | None: ...
    def user_by_id(self, user_id: str) -> UserRecord | None: ...
    def admins(self) -> Sequence[UserRecord]: ...
    def has_role(self, role: str) -> bool: ...
    def restore_notice_active(self) -> bool: ...
    def password_login_suspended(self) -> bool: ...
    def insert_user(
        self,
        *,
        id: str,
        username: str,
        role: str,
        password_hash: str,
        now: datetime,
        max_age_rating: int | None = None,
    ) -> None: ...
    def replace_password_hash(self, user_id: str, expected: str, password_hash: str) -> bool:
        """The new hash of the SAME password, written only while the stored one is still
        `expected`: False when a reset (or anything else) changed it meanwhile, nothing written."""
        ...

    def delete_user(
        self, user_id: str, *, keep_one_admin: bool = False, actor_id: str | None = None
    ) -> str | None:
        """`keep_one_admin`: the last admin is refused (`ProfileError("last_admin")`), decided in
        the transaction that deletes."""
        ...

    def export_rows(self, user_id: str, *, limit: int) -> dict[str, Any] | None:
        """What is hers, column by column and without any secret, or None if there is no such user
        (M3 L08, EF-USR-013)."""
        ...

    # --- household profiles (M3 L05) ---
    def profiles(self) -> Sequence[ProfileRecord]: ...
    def profile(self, user_id: str) -> ProfileRecord | None: ...
    def create_profile(
        self,
        *,
        id: str,
        username: str,
        role: str,
        password_hash: str,
        max_age_rating: int | None,
        can_rate: bool,
        now: datetime,
        max_admins: int,
        actor_id: str | None = None,
    ) -> None:
        """`ProfileError`: `username_taken`, `too_many_admins` (counted in the transaction)."""
        ...

    def update_profile(
        self,
        user_id: str,
        *,
        expected_role: str,
        expected_max_age_rating: int | None,
        expected_can_rate: bool,
        role: str,
        max_age_rating: int | None,
        can_rate: bool,
        max_admins: int,
        now: datetime,
        actor_id: str | None = None,
    ) -> ProfileRecord:
        """One transaction, answering with what it wrote: `ProfileError` `changed_meanwhile` (the
        role moved since it was read),
        `last_admin`, `too_many_admins`; the same `changed_meanwhile` when the ceiling or the
        right to classify differ from what was read. `actor_id`: an admin caller is re-read under
        the lock (`not_allowed`). A change of role closes the sessions and keys of the profile and
        drops its stored policies (D14 again)."""
        ...

    def visibility_state(self, user_id: str) -> tuple[str, str | None] | None:
        """`(role, stored visibility)`; None: no such profile."""
        ...

    def set_visibility(
        self,
        user_id: str,
        visibility: object,
        *,
        actor_id: str | None = None,
        by_admin: bool = False,
        audit: Callable[[str, str], AuditEntry | None] | None = None,
    ) -> tuple[str, str]:
        """`(visibility that held, visibility that holds)`, the role read under the lock that
        writes; `ProfileError` of the rule that refuses; `not_allowed` when `by_admin` and the
        caller is no longer an admin."""
        ...

    def set_family_search(
        self,
        user_id: str,
        hide_unrated_remote: bool,
        subject_filter: tuple[str, ...],
        *,
        actor_id: str | None = None,
        audit: Callable[[ProfileRecord, ProfileRecord], AuditEntry | None] | None = None,
    ) -> tuple[ProfileRecord, ProfileRecord]:
        """Store the family search settings of a profile in one transaction with its audit entry:
        `(record that held, record that holds)`. The admin caller is re-read under the lock
        (`not_allowed`); `not_found` for a profile that is not there."""
        ...

    def set_kavita_username(
        self,
        user_id: str,
        name: str | None,
        *,
        actor_id: str | None = None,
        audit: Callable[[bool, bool], AuditEntry | None] | None = None,
    ) -> tuple[bool, bool]:
        """Name or clear the Kavita account of a child in one transaction with its audit entry:
        `(was mapped, is mapped)`. The admin caller and the role are re-read under the lock
        (`not_allowed`, `not_found`, `not_a_child`); a name held by another profile is
        `kavita_username_taken`."""
        ...

    def child_kavita_accounts(self) -> Sequence[ChildKavitaAccount]: ...
    def source_family(self, source_key: str) -> bool: ...
    def set_source_policy(
        self,
        user_id: str,
        source_key: str,
        policy: str,
        *,
        now: datetime,
        actor_id: str | None = None,
    ) -> SourcePolicyRecord:
        """Role and family mark re-read under the lock: `ProfileError` of the rule that refuses.
        Answers with the row written."""
        ...

    def source_policy_state(
        self, user_id: str, source_key: str
    ) -> tuple[str, str | None, bool] | None:
        """`(role, stored policy, family)` read in one statement; None: no such profile."""
        ...

    def stored_source_policies(self, user_id: str) -> Mapping[str, str]: ...

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
    def live_sessions(self, *, now: datetime, user_id: str | None = None) -> Sequence[SessionView]:
        """The sessions neither revoked nor expired, most recently active first."""
        ...

    def revoke_session_of(self, user_id: str, session_id: str, *, now: datetime) -> bool:
        """One statement on (owner, id): a session of someone else is False, like an unknown one."""
        ...

    def insert_api_key(self, key: NewApiKey) -> None: ...
    def api_key_by_prefix(self, prefix: str) -> ApiKeyRecord | None: ...
    def api_keys(self) -> Sequence[ApiKeyRecord]: ...
    def touch_api_key(self, key_id: str, *, now: datetime) -> None: ...
    def revoke_api_key(self, prefix: str, *, now: datetime) -> bool: ...
    def rotate_api_key(self, old_prefix: str, new: NewApiKey, *, audit: AuditEntry) -> bool:
        """Revoke the live key `old_prefix`, store `new` and the audit line, all or nothing.
        False: no live key has that prefix (nothing written). `KeyConflictError`: prefix taken."""
        ...

    def insert_passkey(self, passkey: NewPasskey, audit: AuditEntry | None = None) -> bool:
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

    def passkey_registrations_since_acknowledgement(self) -> list[tuple[str, str, str, datetime]]:
        """`(reference, username, passkey name, registered at)` for each registration audited
        after the last `passkey.registrations_acknowledged` entry, oldest first; the reference is
        the id of the audit line of the registration."""
        ...

    def passkey_rp_ids(self) -> set[str]:
        """The RP IDs of the live passkeys that know theirs."""
        ...

    def reset_credentials(
        self, user_id: str, password_hash: str, *, now: datetime
    ) -> tuple[int, int, int]:
        """New password, every session closed, every passkey and API key revoked: one
        transaction. Returns (sessions closed, passkeys revoked, API keys revoked)."""
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
