"""Local authentication: admin account, server sessions, API keys, re-authentication.

Invariants this module keeps (EF-USR-021, EF-USR-022, EF-USR-003, ENF-SEC-008):
- the database holds digests of tokens and keys, never the tokens or keys themselves;
- a login always opens a new session with a new token, and closes the one it was sent with
  (no fixation); a re-authentication renews the token and the CSRF secret;
- an unknown user and a wrong password cost the same argon2 verification and give the same error;
- a throttled attempt is refused before any password is checked.
"""

import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import structlog

from shelfhold.identity.application.passkeys import (
    PasskeyCeremony,
    PasskeyError,
    PasskeyOptions,
    PasskeyRefusedError,
    PasskeyUnavailableError,
)
from shelfhold.identity.application.ports import (
    ApiKeyRecord,
    AuditEntry,
    ChildKavitaAccount,
    DeviceProofPort,
    DeviceRecord,
    DeviceTokenRecord,
    IdentityStore,
    KeyConflictError,
    LiteSessionPort,
    NewApiKey,
    NewDeviceToken,
    NewSession,
    OidcIdentityView,
    OidcLastUnlink,
    OidcLink,
    OidcUnlink,
    PasskeyGoneError,
    PasskeyRecord,
    PasswordHasherPort,
    ProfileRecord,
    ProofWithdrawnError,
    ReauthMethod,
    SourcePolicyRecord,
    UserRecord,
)
from shelfhold.identity.application.profiles import ProfileService
from shelfhold.identity.application.sessions import SessionView, device_label
from shelfhold.identity.domain.credentials import (
    HARDENED_KEY_MAX_AGE,
    MAX_PASSWORD_LENGTH,
    SESSION_TOUCH_INTERVAL,
    Credential,
    Principal,
    SessionPolicy,
    SessionTimes,
    check_password_shape,
    initial_expiry,
    session_expired,
    session_policy,
    slid_expiry,
)
from shelfhold.identity.domain.device import (
    DeviceKindError,
    device_kind,
    device_name,
    may_handle,
)
from shelfhold.identity.domain.device_secret import (
    PUBLIC_ID_LENGTH,
    key_digest,
    new_device_credential,
    secret_digest,
)
from shelfhold.identity.domain.device_token import (
    device_token,
    device_token_is_live,
    token_principal,
)
from shelfhold.identity.domain.reserved import (
    DEFAULT_FRESH_AUTH_S,
    DEFAULT_RESERVED_ROLES,
    Allowed,
    ReauthRequired,
    ReservedAction,
    Verdict,
    authorize_reserved,
)
from shelfhold.identity.domain.roles import ProfileError
from shelfhold.identity.domain.throttle import LoginThrottle, address_key
from shelfhold.identity.domain.tokens import (
    new_api_key,
    new_token,
    parse_api_key,
    same_secret,
    token_digest,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid

ADMIN_ROLE = "admin"
# The door of the first account counts its wrong secrets under one key, whatever the address
SETUP_THROTTLE_KEY = "\0setup-token"
IDENTITY_HEADER_AUDIT = "auth.identity_header_stripped"  # read back by `shelfhold status`
FORWARD_REJECTED_AUDIT = (
    "auth.forward_rejected_route"  # EF-USR-002: identity on a route with its own credentials
)
KEY_SCOPES = (
    "read",
    "request",
    "admin",
    "metrics",  # ENF-OBS-002: reads `/metrics` and nothing else; an admin's own, for herself
)  # `destructive` is reserved, not implemented (EF-USR-024 (6))
API_KEY_TOUCH_INTERVAL = timedelta(minutes=1)
OPTIONS_PER_MINUTE = 30  # passkey sign-in options per address
# ADR-0053 §4 (MIN-4): wrong device-token secrets, per (address, identifier) and per address alone
DEVICE_FAILURES_PER_PAIR = 5
DEVICE_FAILURES_PER_ADDRESS = 30
_NO_TOKEN_DIGEST = "sha256:" + "0" * 64  # compared when the identifier is unknown: same work


class AuthError(Exception):
    """Base of the expected refusals; the message is a stable code."""


class InvalidCredentialsError(AuthError):
    def __init__(self) -> None:
        super().__init__("invalid_credentials")


class ThrottledError(AuthError):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__("too_many_attempts")
        self.retry_after_s = retry_after_s


class RememberNeedsConfirmationError(AuthError):
    """The hardened profile keeps no "remember me" session without an explicit confirmation
    (EF-USR-022). Raised after the password is checked, so it tells nothing to a guesser."""

    def __init__(self) -> None:
        super().__init__("remember_needs_confirmation")


class PasswordLoginSuspendedError(AuthError):
    """After a restore that could not carry the passwords over: a password changed since the
    backup is back to its old value, so no password signs in until the admin has reset the
    accounts from the host (`shelfhold admin reset-password`) and run `restore acknowledge`."""

    def __init__(self) -> None:
        super().__init__("password_login_suspended")


class AdminExistsError(AuthError):
    def __init__(self) -> None:
        super().__init__("admin_exists")


class SetupTokenInvalidError(AuthError):
    def __init__(self) -> None:
        super().__init__("setup_token_invalid")


class SetupTokenNotConfiguredError(AuthError):
    def __init__(self) -> None:
        super().__init__("setup_token_not_configured")


class LastAdminError(AuthError):
    def __init__(self) -> None:
        super().__init__("last_admin")


class NotAllowedError(AuthError):
    def __init__(self) -> None:
        super().__init__("not allowed")


class UnknownSessionError(AuthError):
    def __init__(self) -> None:
        super().__init__("no such session")


class UnknownUserError(AuthError):
    def __init__(self) -> None:
        super().__init__("unknown_user")


class UnknownReauthMethodError(AuthError):
    def __init__(self) -> None:
        super().__init__("unknown_reauth_method")


class LastLoginFactorError(AuthError):
    def __init__(self) -> None:
        super().__init__("last_login_factor")


class UnknownPasskeyError(AuthError):
    def __init__(self) -> None:
        super().__init__("unknown_passkey")


@dataclass(frozen=True)
class PasskeyStatus:
    available: bool
    reason: str | None
    rp_id: str | None


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_secret: str
    principal: Principal
    remember: bool
    cookie_max_age_s: int | None  # None: a browser session cookie
    device_proof: str | None = None  # set at login: this browser succeeded on this account


@dataclass(frozen=True)
class SessionAuth:
    principal: Principal
    csrf_secret: str
    remember: bool
    touched: bool  # the expiry moved: the cookie is sent again
    cookie_max_age_s: int | None


@dataclass(frozen=True)
class CreatedDeviceToken:
    """What is shown once, at pairing. `secret` stays out of the `repr` and `str`."""

    public_id: str
    secret: str = field(repr=False)
    subject_user_id: str
    expires_at: datetime | None
    device_id: str = ""

    def __str__(self) -> str:
        return f"CreatedDeviceToken(public_id={self.public_id!r})"


@dataclass(frozen=True)
class CreatedApiKey:
    value: str  # shown once
    prefix: str
    expires_at: datetime | None


def _may_pair(by: Principal, subject_id: str | None) -> bool:
    """An admin pairs for any profile (the subject's rules apply after), a member for themself."""
    if by.role == "admin":
        return True
    return by.role == "member" and subject_id is not None and subject_id == by.user_id


def _by_host_user(target: str, by: Principal | None = None) -> str:
    """Offline, the CLI has no principal: the audit keeps at least the uid of the host user who
    ran it (review PR #43, M6). Through the local socket (L23) the principal carries the uid and
    the audit actor is `local_cli:<uid>`, so the target stays clean."""
    return target if by is not None else f"{target} uid={os.getuid()}"


def audit_actor(principal: Principal | None) -> str | None:
    if principal is None:
        return None
    return principal.actor_id if principal.credential is Credential.LOCAL_CLI else principal.user_id


def _seconds(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds()))


class PasswordReauth:
    name = "password"

    def __init__(self, hasher: PasswordHasherPort) -> None:
        self._hasher = hasher

    def verify(self, user: UserRecord, payload: Mapping[str, object]) -> bool:
        password = payload.get("password")
        if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
            return False
        return self._hasher.verify(user.password_hash, password)


class PasskeyReauth:
    """Re-authentication by passkey (EF-USR-025): the assertion answers a challenge issued for
    this very session, and the credential must belong to the account that is re-authenticating."""

    name = "passkey"

    def __init__(
        self, ceremony: PasskeyCeremony, suspended: Callable[[], bool] = lambda: False
    ) -> None:
        self._ceremony = ceremony
        self._suspended = suspended  # true while a restore warning has passkeys suspended

    def verify(self, user: UserRecord, payload: Mapping[str, object]) -> bool:
        if self._suspended():
            return False
        flow, credential, session_id = (
            payload.get("flow"),
            payload.get("credential"),
            payload.get("session_id"),
        )
        if not (
            isinstance(flow, str)
            and isinstance(credential, Mapping)
            and isinstance(session_id, str)
        ):
            return False
        try:
            self._ceremony.finish_reauth(user, session_id, flow, credential)
        except PasskeyError as refused:
            structlog.get_logger().warning("auth.passkey_refused", reason=refused.reason)
            return False
        return True


class LocalAuth:
    def __init__(
        self,
        store: IdentityStore,
        hasher: PasswordHasherPort,
        clock: Clock,
        *,
        hardened: bool = False,
        fresh_auth_s: int = DEFAULT_FRESH_AUTH_S,
        throttle: LoginThrottle | None = None,
        reauth_methods: Sequence[ReauthMethod] | None = None,
        device_proof: DeviceProofPort | None = None,
        lite_session: LiteSessionPort | None = None,
        passkeys: PasskeyCeremony | None = None,
        on_passkey_registered: Callable[[PasskeyRecord], None] | None = None,
    ) -> None:
        self._store = store
        # Owner decision 2026-11-12: told after every registration, on any account (the event)
        self._on_passkey_registered = on_passkey_registered
        self._hasher = hasher
        self._clock = clock
        self.hardened = hardened
        self.fresh_auth_s = fresh_auth_s
        self._throttle = throttle or LoginThrottle()
        self._device_throttle = LoginThrottle(
            per_address=DEVICE_FAILURES_PER_ADDRESS, per_account=DEVICE_FAILURES_PER_PAIR
        )
        self._device_proof = device_proof
        self._lite = lite_session
        methods = list(reauth_methods if reauth_methods is not None else [PasswordReauth(hasher)])
        self._passkeys = passkeys
        self._options_throttle = LoginThrottle(
            per_address=OPTIONS_PER_MINUTE, per_account=OPTIONS_PER_MINUTE
        )
        # the provider's start has a budget of its own (M5 L04, #362 review): one script spending
        # the passkey options must not close the provider's sign-in, nor the reverse
        self._oidc_start_throttle = LoginThrottle(
            per_address=OPTIONS_PER_MINUTE, per_account=OPTIONS_PER_MINUTE
        )
        if passkeys is not None and passkeys.available:  # the method exists in a secure context
            methods.append(PasskeyReauth(passkeys, store.restore_notice_active))
        self._reauth = {method.name: method for method in methods}
        self.oidc: Any = None  # the OpenID Connect sign-in, set by the factory when configured
        self._profiles = ProfileService(
            store,
            hasher,
            clock.now,
            lambda action, by, target: self._audit(action, by, target=target),
            lambda action, by, target: self._entry(action, by, target),
        )

    @property
    def reauth_method_names(self) -> tuple[str, ...]:
        return tuple(self._reauth)

    def now(self) -> datetime:
        return self._clock.now()

    def _policy(self, role: str) -> SessionPolicy:
        return session_policy(role, hardened=self.hardened)

    def _entry(
        self, action: str, principal: Principal | None = None, target: str | None = None
    ) -> AuditEntry:
        return AuditEntry(
            actor_id=audit_actor(principal),
            action=action,
            at=self.now(),
            target=target,
            session_id=principal.session_id if principal else None,
            auth_time=principal.auth_time if principal else None,
        )

    def _audit(
        self, action: str, principal: Principal | None = None, *, target: str | None = None
    ) -> None:
        """Offline host CLI actions have no principal; through the local socket the principal is
        `local_cli` and the actor is `local_cli:<uid>` (L23)."""
        entry = self._entry(action, principal, target)
        self._store.audit(entry)
        # INFO for the actions that change who can do what; never the target (a key prefix...)
        structlog.get_logger().info(
            "auth.audit",
            action=action,
            actor_id=entry.actor_id,
            role=getattr(principal, "role", None),
        )

    # --- accounts (CLI) ----------------------------------------------------------------------

    def create_admin(self, username: str, password: str, *, by: Principal | None = None) -> str:
        username = username.strip()
        if not username or len(username) > 128:
            raise ValueError("invalid_username")
        check_password_shape(password)
        if self._store.admins():
            raise AdminExistsError  # one local admin in M1a (EF-USR-021)
        user_id = new_ulid()
        if not self._store.insert_user(
            id=user_id,
            username=username,
            role=ADMIN_ROLE,
            password_hash=self._hasher.hash(password),
            now=self.now(),
            only_if_no_admin=True,
        ):
            raise AdminExistsError
        self._audit("user.created", by, target=user_id)
        return user_id

    def needs_admin(self) -> bool:
        """True on a new instance: no administrator yet."""
        return not self._store.admins()

    def bootstrap_admin(
        self,
        username: str,
        password: str,
        *,
        presented_token: str,
        expected_token: str | None,
        ip: str,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """The first account of a new instance, made from the wizard (M3 ENF-UX-001, EF-UX-001).

        Open only while no administrator exists and only to the one who holds the secret that the
        compose gave to the instance. A guess is counted per address, like a wrong password and
        before the secret is looked at (the secret is long and generated: the limit of an address
        is plenty, and a limit shared by all addresses would let anyone shut the door on the
        installer). The account is made by `create_admin` (one transaction with the check that
        none exists: a race makes one owner), then signed in as `login` signs in."""
        if self._store.admins():
            raise AdminExistsError
        if not expected_token:
            raise SetupTokenNotConfiguredError
        at = self._reserve_attempt(ip=ip, account=SETUP_THROTTLE_KEY, skip_account=True)
        if not same_secret(presented_token, expected_token):
            structlog.get_logger().warning("auth.setup_token_refused", ip=ip)
            raise SetupTokenInvalidError
        self._throttle.release(ip=ip, account=SETUP_THROTTLE_KEY, at=at, skip_account=True)
        user = self._store.user_by_id(self.create_admin(username, password))
        if user is None or user.password_hash is None:  # pragma: no cover - just inserted
            raise AdminExistsError
        return self._open_password_session(
            user, user.password_hash, remember=False, ip=ip, user_agent=user_agent
        )

    def delete_user(self, username: str, *, by: Principal | None = None) -> str:
        """Delete an account; its audit entries are kept under a pseudonym (EF-USR-013). The
        last admin cannot go: nobody could open the instance again. Returns the pseudonym."""
        user = self._store.user_by_name(username)
        if user is None:
            raise UnknownUserError
        if user.role == ADMIN_ROLE and len(self._store.admins()) <= 1:
            raise LastAdminError
        try:
            pseudonym = self._store.delete_user(user.id, keep_one_admin=True)
        except ProfileError:  # the last admin, decided in the transaction that deletes
            raise LastAdminError from None
        if pseudonym is None:
            raise UnknownUserError
        self._audit("user.deleted", by, target=pseudonym)
        return pseudonym

    def reset_password(self, username: str, password: str, *, by: Principal | None = None) -> int:
        """New password; every session, passkey and API key of the account closed. Returns the
        count of sessions closed."""
        check_password_shape(password)
        user = self._store.user_by_name(username)
        if user is None:
            raise UnknownUserError
        # One transaction: the password, the sessions and the passkeys change together, so a
        # passkey sign-in cannot slip between the steps (a lost or stolen device is the usual
        # reason for a reset, ADR-0050: its passkeys go too).
        closed, revoked, keys = self._store.reset_credentials(
            user.id, self._hasher.hash(password), now=self.now()
        )
        self._audit("user.password_reset", by, target=user.id)
        if revoked:
            self._audit("user.passkeys_revoked", by, target=user.id)
        if keys:  # a key left on a lost device must not read on after the reset
            self._audit("user.api_keys_revoked", by, target=user.id)
        return closed

    # --- household profiles (M3 L05): see `profiles.py` --------------------------------------

    def profile_add(
        self,
        username: str,
        password: str,
        role: str,
        *,
        max_age_rating: int | None = None,
        can_rate: bool | None = None,
        by: Principal | None = None,
    ) -> str:
        return self._profiles.add(
            username, password, role, max_age_rating=max_age_rating, can_rate=can_rate, by=by
        )

    def profile_add_record(
        self,
        username: str,
        password: str,
        role: str,
        *,
        max_age_rating: int | None = None,
        can_rate: bool | None = None,
        by: Principal | None = None,
    ) -> ProfileRecord:
        return self._profiles.add_profile(
            username, password, role, max_age_rating=max_age_rating, can_rate=can_rate, by=by
        )

    def profile_list(self, *, by: Principal | None = None) -> Sequence[ProfileRecord]:
        return self._profiles.list(by=by)

    def profile_set(
        self,
        profile_id: str,
        *,
        role: str | None = None,
        max_age_rating: int | None = None,
        can_rate: bool | None = None,
        by: Principal | None = None,
    ) -> ProfileRecord:
        return self._profiles.set(
            profile_id, role=role, max_age_rating=max_age_rating, can_rate=can_rate, by=by
        )

    def profile_remove(self, profile_id: str, *, by: Principal | None = None) -> None:
        self._profiles.remove(profile_id, by=by)

    def profile_export(self, profile_id: str, *, by: Principal | None = None) -> dict[str, Any]:
        return self._profiles.export(profile_id, by=by)

    def profile_policy_set(
        self, profile_id: str, source_key: str, policy: str, *, by: Principal | None = None
    ) -> SourcePolicyRecord:
        return self._profiles.set_source_policy(profile_id, source_key, policy, by=by)

    def profile_policies(
        self, profile_id: str, *, by: Principal | None = None
    ) -> Sequence[SourcePolicyRecord]:
        return self._profiles.source_policies(profile_id, by=by)

    def effective_source_policy(self, profile_id: str, source_key: str) -> str:
        return self._profiles.effective_source_policy(profile_id, source_key)

    def visibility_of(self, profile_id: str) -> str:
        return self._profiles.visibility_of(profile_id)

    def profile_set_visibility(
        self, profile_id: str, visibility: object, *, by: Principal | None = None
    ) -> str:
        return self._profiles.set_visibility(profile_id, visibility, by=by)

    def profile_set_family_search(
        self,
        profile_id: str,
        hide_unrated_remote: bool,
        subject_filter: tuple[str, ...],
        *,
        by: Principal | None = None,
    ) -> ProfileRecord:
        return self._profiles.set_family_search(
            profile_id, hide_unrated_remote, subject_filter, by=by
        )

    def profile_set_kavita_username(
        self, profile_id: str, name: object, *, by: Principal | None = None
    ) -> str | None:
        return self._profiles.set_kavita_username(profile_id, name, by=by)

    def child_kavita_accounts(self, *, by: Principal | None = None) -> Sequence[ChildKavitaAccount]:
        return self._profiles.child_kavita_accounts(by=by)

    def set_own_visibility(self, by: Principal, visibility: object) -> str:
        return self._profiles.set_own_visibility(by, visibility)

    # --- sessions ----------------------------------------------------------------------------

    def _issue(
        self,
        user: UserRecord,
        *,
        remember: bool,
        ip: str | None,
        passkey_id: str | None = None,
        user_agent: str | None = None,
        password_hash: str | None = None,
        auth_time: datetime | None = None,
        oidc: tuple[str, str] | None = None,
        proof: str = "local",
    ) -> IssuedSession:
        now = self.now()
        policy = self._policy(user.role)
        token, csrf = new_token(), new_token()
        session_id = new_ulid()
        self._store.insert_session(
            NewSession(
                id=session_id,
                user_id=user.id,
                token_hash=token_digest(token),
                csrf_secret=csrf,
                now=now,
                absolute_expiry=initial_expiry(policy, now),
                remember=remember,
                ip=ip,
                passkey_id=passkey_id,
                password_hash=password_hash,
                device=device_label(user_agent),
                auth_time=auth_time,
                oidc=oidc,
                proof=proof,
            )
        )
        principal = Principal(
            user_id=user.id,
            username=user.username,
            role=user.role,
            credential=Credential.SESSION,
            session_id=session_id,
            auth_time=auth_time or now,
            can_rate=user.can_rate,
            locale=user.locale,
            provider_proof=proof == "provider",
        )
        return IssuedSession(
            token=token,
            csrf_secret=csrf,
            principal=principal,
            remember=remember,
            cookie_max_age_s=_seconds(policy.idle) if remember else None,
        )

    def _reserve_attempt(self, *, ip: str, account: str, skip_account: bool = False) -> datetime:
        """Count the attempt before the password is checked (atomic); refuse when throttled."""
        at = self.now()
        wait = self._throttle.reserve(ip=ip, account=account, now=at, skip_account=skip_account)
        if wait is not None:
            raise ThrottledError(max(1, math.ceil(wait)))
        return at

    def _fail(self, *, ip: str) -> InvalidCredentialsError:
        structlog.get_logger().warning("auth.login_failed", ip=ip)  # never the username tried
        return InvalidCredentialsError()

    def login(
        self,
        username: str,
        password: str,
        *,
        ip: str,
        remember: bool = True,
        confirm_remember: bool = False,
        presented_token: str | None = None,
        device_proof: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        if self._store.password_login_suspended():
            raise PasswordLoginSuspendedError
        known_device = self._device_proof is not None and self._device_proof.verify(
            device_proof, username, self.now()
        )
        at = self._reserve_attempt(ip=ip, account=username, skip_account=known_device)
        user = self._store.user_by_name(username)
        # Same argon2 cost whether the user exists, has no password, or typed it wrong.
        valid = self._hasher.verify(user.password_hash if user else None, password)
        if user is None or user.password_hash is None or not valid:
            raise self._fail(ip=ip)
        self._throttle.release(ip=ip, account=username, at=at, skip_account=known_device)
        if remember and not confirm_remember and not self._policy(user.role).remember_default:
            raise RememberNeedsConfirmationError  # the password was right: nothing is opened
        checked_hash = user.password_hash
        if self._hasher.needs_rehash(user.password_hash):
            checked_hash = self._hasher.hash(password)
            # only while the hash that was checked is still the stored one: a reset that
            # committed since is never overwritten, and answers as a wrong password
            if not self._store.replace_password_hash(user.id, user.password_hash, checked_hash):
                raise self._fail(ip=ip)
        if presented_token:  # whatever the browser held is closed, never adopted
            self._store.revoke_session_by_token_hash(token_digest(presented_token), now=self.now())
        return self._open_password_session(
            user, checked_hash, remember=remember, ip=ip, user_agent=user_agent
        )

    def _open_password_session(
        self,
        user: UserRecord,
        checked_hash: str,
        *,
        remember: bool,
        ip: str,
        user_agent: str | None,
    ) -> IssuedSession:
        """The session of a password that was just checked (sign-in, first account): tied to that
        hash, read again under the write lock (a reset in between refuses), with the proof of the
        device and the audit line."""
        try:
            issued = self._issue(
                user, remember=remember, ip=ip, user_agent=user_agent, password_hash=checked_hash
            )
        except ProofWithdrawnError:
            raise self._fail(ip=ip) from None
        self._audit("auth.login", issued.principal)
        if self._device_proof is not None:
            issued = replace(
                issued, device_proof=self._device_proof.issue(user.username, self.now())
            )
        return issued

    def authenticate_session(self, token: str) -> SessionAuth | None:
        record = self._store.session_by_token_hash(token_digest(token))
        if record is None or record.revoked_at is not None:
            return None
        now = self.now()
        policy = self._policy(record.user.role)
        times = SessionTimes(record.created_at, record.last_seen_at, record.absolute_expiry)
        if session_expired(times, policy, now):
            return None
        expiry = record.absolute_expiry
        touched = now - record.last_seen_at >= SESSION_TOUCH_INTERVAL
        if touched:
            expiry = slid_expiry(times, policy, now)
            self._store.touch_session(record.id, now=now, absolute_expiry=expiry)
        principal = Principal(
            user_id=record.user.id,
            username=record.user.username,
            role=record.user.role,
            credential=Credential.SESSION,
            session_id=record.id,
            auth_time=record.auth_time,
            can_rate=record.user.can_rate,
            locale=record.user.locale,
            provider_proof=record.proof == "provider",
        )
        max_age = _seconds(min(policy.idle, expiry - now)) if record.remember else None
        return SessionAuth(
            principal=principal,
            csrf_secret=record.csrf_secret,
            remember=record.remember,
            touched=touched,
            cookie_max_age_s=max_age,
        )

    def logout(self, principal: Principal) -> None:
        if principal.session_id is not None:
            self._store.revoke_sessions(now=self.now(), session_id=principal.session_id)
            self._audit("auth.logout", principal)

    def revoke_sessions(
        self,
        *,
        username: str | None = None,
        session_id: str | None = None,
        by: Principal | None = None,
    ) -> int:
        user_id = None
        if username is not None:
            user = self._store.user_by_name(username)
            if user is None:
                raise UnknownUserError
            user_id = user.id
        closed = self._store.revoke_sessions(now=self.now(), user_id=user_id, session_id=session_id)
        self._audit("auth.sessions_revoked", by, target=session_id or user_id or "*")
        return closed

    # --- the list of sessions and their revocation (M3 L07, EF-USR-011) ----------------------
    def _live(self, views: Sequence[SessionView]) -> list[SessionView]:
        """Without the sessions a request would already refuse: idle past the limit of the role,
        past the absolute cap, or expired (the same rule as `authenticate_session`)."""
        now = self.now()
        return [
            v
            for v in views
            if not session_expired(
                SessionTimes(v.created_at, v.last_seen_at, v.absolute_expiry),
                self._policy(v.role),
                now,
            )
        ]

    def _require_admin(self, principal: Principal) -> None:
        """The service refuses a second time (L05, decision 2): an admin session that the database
        still holds as one, or the host. A key, a token, a member, a child: never."""
        if principal.is_host:
            if principal.role == "admin":
                return
        elif principal.credential is Credential.SESSION and principal.role == "admin":
            current = self._store.profile(principal.user_id)
            if current is not None and current.role == "admin":
                return
        raise NotAllowedError

    def sessions_list(self, principal: Principal) -> Sequence[SessionView]:
        """The caller's own live sessions: the owner comes from the principal, never a request."""
        return self._live(self._store.live_sessions(now=self.now(), user_id=principal.user_id))

    def sessions_list_all(self, principal: Principal) -> Sequence[SessionView]:
        """Every profile's live sessions; the route's reserved-action guard writes the audit."""
        self._require_admin(principal)
        return self._live(self._store.live_sessions(now=self.now()))

    def session_revoke(self, principal: Principal, session_id: str) -> None:
        """Close one session of the caller's own; another profile's is `UnknownSessionError`."""
        if not self._store.revoke_session_of(principal.user_id, session_id, now=self.now()):
            raise UnknownSessionError
        self._audit("auth.session_revoked", principal, target=session_id)

    def sessions_revoke_others(self, principal: Principal) -> int:
        """Close the caller's own live sessions but the one in use; one audit line for each
        session closed, none when nothing was."""
        closed = 0
        for view in self.sessions_list(principal):
            if view.id == principal.session_id:
                continue
            if self._store.revoke_session_of(principal.user_id, view.id, now=self.now()):
                self._audit("auth.session_revoked", principal, target=view.id)
                closed += 1
        return closed

    def session_revoke_of_other(self, principal: Principal, session_id: str) -> None:
        """Close any live session by its id. The audit line names the account it belonged to and
        is written only when a session was closed (`session.revoke_other`, with the caller's
        session and `auth_time`)."""
        self._require_admin(principal)
        owner = next(
            (v.user_id for v in self._store.live_sessions(now=self.now()) if v.id == session_id),
            None,
        )
        if owner is None or not self._store.revoke_sessions(now=self.now(), session_id=session_id):
            raise UnknownSessionError
        self._audit("session.revoke_other", principal, target=f"user={owner} session={session_id}")

    def reauthenticate(
        self, principal: Principal, method: str, payload: Mapping[str, object], *, ip: str
    ) -> IssuedSession:
        """Fresh `auth_time`, new token and new CSRF secret on the same session row."""
        if principal.credential is not Credential.SESSION or principal.session_id is None:
            raise InvalidCredentialsError
        verifier = self._reauth.get(method)
        if verifier is None:
            raise UnknownReauthMethodError
        at = self._reserve_attempt(ip=ip, account=principal.username)
        user = self._store.user_by_id(principal.user_id)
        # The session is the server's to name: whatever the body said is overwritten.
        proof = {**payload, "session_id": principal.session_id}
        if user is None or not verifier.verify(user, proof):
            raise self._fail(ip=ip)
        self._throttle.release(ip=ip, account=principal.username, at=at)
        now = self.now()
        token, csrf = new_token(), new_token()
        self._store.rotate_session(
            principal.session_id,
            token_hash=token_digest(token),
            csrf_secret=csrf,
            now=now,
            proof="local",
        )
        renewed = replace(principal, auth_time=now, provider_proof=False)
        self._audit("auth.reauth", renewed, target=method)
        record = self._store.session_by_token_hash(token_digest(token))
        remember = record.remember if record else True
        policy = self._policy(principal.role)
        return IssuedSession(
            token=token,
            csrf_secret=csrf,
            principal=renewed,
            remember=remember,
            cookie_max_age_s=_seconds(policy.idle) if remember else None,
        )

    # --- passkeys (EF-USR-025, ADR-0050) -------------------------------------------------------

    def _ceremony(self) -> PasskeyCeremony:
        if self._passkeys is None:
            raise PasskeyUnavailableError("no_public_url")
        return self._passkeys

    def _refuse_while_restore_pending(self) -> None:
        """After a restore a passkey revoked since the backup is live again: no passkey signs in
        until the admin has looked at them and run `restore acknowledge`."""
        if self._store.restore_notice_active():
            raise PasskeyUnavailableError("restore_pending")

    def passkey_status(self) -> PasskeyStatus:
        ceremony = self._passkeys
        if ceremony is None:
            return PasskeyStatus(False, "no_public_url", None)
        return PasskeyStatus(ceremony.available, ceremony.unavailable_reason, ceremony.rp_id)

    @property
    def passkeys_available(self) -> bool:
        return self._passkeys is not None and self._passkeys.available

    def _account(self, principal: Principal) -> UserRecord:
        user = self._store.user_by_id(principal.user_id)
        if user is None or principal.session_id is None:
            raise InvalidCredentialsError
        return user

    def stale_passkey_rp_ids(self, current_rp_id: str | None) -> list[str]:
        """RP IDs that live passkeys are bound to and that are not the instance's (ADR-0050,
        "change of domain"): those passkeys can no longer be used."""
        return sorted(rp for rp in self._store.passkey_rp_ids() if rp != current_rp_id)

    def passkeys_of(self, principal: Principal) -> Sequence[PasskeyRecord]:
        return self._store.passkeys_of(principal.user_id)

    def passkey_registration_options(self, principal: Principal) -> PasskeyOptions:
        user = self._account(principal)
        return self._ceremony().registration_options(user, str(principal.session_id))

    def passkey_register(
        self, principal: Principal, flow: str, credential: Mapping[str, object], name: str
    ) -> PasskeyRecord:
        user = self._account(principal)
        try:
            passkey = self._ceremony().finish_registration(
                user,
                str(principal.session_id),
                flow,
                credential,
                name,
                # the audit line is written with the passkey, in one transaction
                audit_for=lambda passkey_id: self._entry(
                    "passkey.registered", principal, passkey_id
                ),
            )
        except PasskeyRefusedError as refused:
            structlog.get_logger().warning("auth.passkey_refused", reason=refused.reason)
            raise
        structlog.get_logger().info(
            "auth.audit",
            action="passkey.registered",
            actor_id=audit_actor(principal),
            role=getattr(principal, "role", None),
        )
        if self._on_passkey_registered is not None:
            try:
                self._on_passkey_registered(passkey)
            except Exception:  # the status line is read from the audit: the event is a plus
                structlog.get_logger().warning("auth.passkey_event_failed")
        return passkey

    def unacknowledged_passkeys(self) -> list[tuple[str, str, str, datetime]]:
        """The registrations `status` shows until `acknowledge_passkeys` (owner decision
        2026-11-12): `(passkey id, username, name, registered at)`."""
        return self._store.passkey_registrations_since_acknowledgement()

    def acknowledge_passkeys(self, through: str, by: Principal | None = None) -> int:
        """The admin has looked at the registrations of `status`, up to the reference it showed
        last (`through`): one audit line closes them, and what registered after stays listed. The
        count acknowledged. `ValueError("unknown_reference")`: no waiting registration has it."""
        waiting = self._store.passkey_registrations_since_acknowledgement()
        if through not in {reference for reference, *_ in waiting}:
            raise ValueError("unknown_reference")
        covered = [reference for reference, *_ in waiting if reference <= through]
        self._audit("passkey.registrations_acknowledged", by, target=through)
        return len(covered)

    def passkey_revoke(self, principal: Principal, passkey_id: str) -> None:
        outcome = self._store.revoke_passkey(principal.user_id, passkey_id, now=self.now())
        if outcome == "last_factor":
            raise LastLoginFactorError
        if outcome != "revoked":
            raise UnknownPasskeyError
        self._audit("passkey.revoked", principal, target=passkey_id)

    def passkey_reauth_options(self, principal: Principal) -> PasskeyOptions:
        self._refuse_while_restore_pending()
        user = self._account(principal)
        return self._ceremony().reauth_options(user, str(principal.session_id))

    def passkey_login_options(self, *, ip: str) -> PasskeyOptions:
        """Anonymous and cheap to call: each address gets a few per minute (429 beyond)."""
        self._refuse_while_restore_pending()
        ceremony = self._ceremony()
        wait = self._options_throttle.reserve(ip=ip, account=ip, now=self.now())
        if wait is not None:
            raise ThrottledError(max(1, math.ceil(wait)))
        return ceremony.login_options()

    def passkey_login(
        self,
        flow: str,
        credential: Mapping[str, object],
        *,
        ip: str,
        binder: str = "",
        remember: bool = True,
        confirm_remember: bool = False,
        presented_token: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Discoverable login: the credential names the account. Same throttle as a password
        (every attempt is counted before it is checked), same answer for every refusal, a new
        session whatever the browser held (no fixation), `auth_time` = now."""
        self._refuse_while_restore_pending()
        ceremony = self._ceremony()
        account = f"passkey:{ip}"  # no account is known before the assertion: one key per address
        at = self._reserve_attempt(ip=ip, account=account)
        try:
            user, passkey = ceremony.finish_login(flow, binder, credential)
        except PasskeyError as refused:
            structlog.get_logger().warning("auth.passkey_refused", reason=refused.reason, ip=ip)
            raise self._fail(ip=ip) from None
        self._throttle.release(ip=ip, account=account, at=at)
        if remember and not confirm_remember and not self._policy(user.role).remember_default:
            raise RememberNeedsConfirmationError
        if presented_token:  # whatever the browser held is closed, never adopted
            self._store.revoke_session_by_token_hash(token_digest(presented_token), now=self.now())
        try:
            issued = self._issue(
                user, remember=remember, ip=ip, passkey_id=passkey.id, user_agent=user_agent
            )
        except PasskeyGoneError:  # revoked between the assertion and the session
            raise self._fail(ip=ip) from None
        self._audit("auth.passkey_login", issued.principal, target=passkey.id)
        return issued

    # --- OpenID Connect (M5 L03, EF-USR-004) ----------------------------------------------------

    @property
    def oidc_available(self) -> bool:
        return self.oidc is not None

    def reserve_options(self, *, ip: str) -> None:
        """A cheap anonymous step (`ThrottledError` beyond a few a minute per address)."""
        wait = self._options_throttle.reserve(ip=ip, account=ip, now=self.now())
        if wait is not None:
            raise ThrottledError(max(1, math.ceil(wait)))

    def reserve_oidc_start(self, *, ip: str) -> None:
        """The start of a sign-in at the provider: the same ceiling as passkey options, counted
        apart from them (`ThrottledError`)."""
        wait = self._oidc_start_throttle.reserve(ip=ip, account=ip, now=self.now())
        if wait is not None:
            raise ThrottledError(max(1, math.ceil(wait)))

    def reserve_attempt(self, *, ip: str, account: str) -> datetime:
        return self._reserve_attempt(ip=ip, account=account)

    def release_attempt(self, *, ip: str, account: str, at: datetime) -> None:
        self._throttle.release(ip=ip, account=account, at=at)

    def open_federated_session(
        self,
        user: UserRecord,
        *,
        ip: str,
        user_agent: str | None,
        presented_token: str | None,
        auth_time: datetime,
        oidc: tuple[str, str] | None = None,
    ) -> IssuedSession:
        """A session for a person an identity provider vouched for: new token whatever the browser
        held (no fixation), `auth_time` is what the provider said about when they signed in there
        (never `now`: no local secret was typed), the policy of the role for `remember`."""
        if presented_token:  # whatever the browser held is closed, never adopted
            self._store.revoke_session_by_token_hash(token_digest(presented_token), now=self.now())
        remember = self._policy(user.role).remember_default
        try:
            issued = self._issue(
                user, remember=remember, ip=ip, user_agent=user_agent, auth_time=auth_time,
                oidc=oidc, proof="provider",
            )  # fmt: skip
        except ProofWithdrawnError:  # unlinked, or turned into a child profile, meanwhile
            raise InvalidCredentialsError from None
        self._audit("auth.oidc_login", issued.principal)
        return issued

    def link_oidc_identity(
        self,
        *,
        user_id: str,
        issuer: str,
        sub: str,
        session_id: str | None = None,
        by: Principal | None = None,
    ) -> OidcLink:
        """The outcome of one transaction that also writes the audit line (the issuer and the
        account, never the `sub`). `by`: the admin who linked another's account."""
        entry = AuditEntry(
            actor_id=audit_actor(by) or user_id,
            action="oidc.linked",
            at=self.now(),
            target=user_id,
            session_id=by.session_id if by is not None else session_id,
            auth_time=by.auth_time if by is not None else None,
        )
        return self._store.link_oidc_identity(
            user_id=user_id,
            issuer=issuer,
            sub=sub,
            now=self.now(),
            audit=entry,
            admin_actor=by.user_id if by is not None else None,
        )

    def unlink_oidc_identities(self, *, user_id: str, by: Principal) -> OidcUnlink:
        """Removes the links of an account, each in one transaction (which also decides, under the
        write lock, whether this caller may: an admin unlinks her own account or a non-admin's)
        with its audit line, the end of the sessions the provider opened for the account and the
        notice to the holder (also when the holder did it themselves: a stolen fresh session
        could). The caller has passed the reserved action (`oidc.unlink_own`,
        `oidc.unlink_account`)."""
        issuers = [view.issuer for view in self._store.oidc_identities_of(user_id)] or [""]
        outcome = OidcUnlink.NOT_LINKED
        for issuer in issuers:  # none: the store still says whether the account may be asked about
            entry = AuditEntry(
                actor_id=audit_actor(by),
                action="oidc.unlinked",
                at=self.now(),
                target=user_id,
                session_id=by.session_id,
                auth_time=by.auth_time,
            )
            result = self._store.unlink_oidc_identity(
                user_id=user_id,
                issuer=issuer,
                now=self.now(),
                audit=entry,
                by_user_id=by.user_id,
            )
            if result is OidcUnlink.UNLINKED or outcome is OidcUnlink.NOT_LINKED:
                outcome = result
        return outcome

    def oidc_last_unlink(self, principal: Principal) -> OidcLastUnlink | None:
        return self._store.oidc_last_unlink(principal.user_id)

    def reauth_methods_for(self, principal: Principal, *, local_only: bool = False) -> list[str]:
        """The ways this session can be proved again: the local ones, and the provider's when the
        account is linked to it. First for a session the provider opened (it carries no local
        proof), last for any other. `local_only`: the action refuses the provider's proof, so it is
        not offered."""
        methods = list(self._reauth)
        if (
            self.oidc is not None
            and principal.credential is Credential.SESSION
            and not principal.via_forward  # the route refuses it
            and principal.role != "child"
            and self._store.oidc_identity_of(principal.user_id, self.oidc.issuer) is not None
        ):
            if local_only:
                pass
            elif principal.provider_proof:
                methods.insert(0, "oidc")
            else:
                methods.append("oidc")
        return methods

    def renew_session_from_provider(
        self, *, user_id: str, session_id: str, auth_time: datetime
    ) -> IssuedSession | None:
        """The provider signed the person in again (verified by the caller, who gives the moment
        it said): the same session row gets a new token, a new CSRF secret and that `auth_time`.
        None when the session or the account is gone, or the account is a child's."""
        user = self._store.user_by_id(user_id)
        if user is None or user.role == "child":
            return None
        now = self.now()
        token, csrf = new_token(), new_token()
        if not self._store.rotate_session(
            session_id,
            token_hash=token_digest(token),
            csrf_secret=csrf,
            now=now,
            auth_time=auth_time,
            proof="provider",
        ):
            return None
        record = self._store.session_by_token_hash(token_digest(token))
        if record is None or record.revoked_at is not None or record.user.id != user_id:
            return None
        principal = Principal(
            user_id=user.id,
            username=user.username,
            role=user.role,
            credential=Credential.SESSION,
            session_id=session_id,
            auth_time=auth_time,
            can_rate=user.can_rate,
            locale=user.locale,
            provider_proof=True,
        )
        self._audit("auth.reauth", principal, target="oidc")
        policy = self._policy(user.role)
        return IssuedSession(
            token=token,
            csrf_secret=csrf,
            principal=principal,
            remember=record.remember,
            cookie_max_age_s=_seconds(policy.idle) if record.remember else None,
        )

    def oidc_identities_of(self, principal: Principal) -> Sequence[OidcIdentityView]:
        """The links of the caller's own account (never another's): the provider and the date, and
        whether an admin made the link. The identifier itself is not given back."""
        return self._store.oidc_identities_of(principal.user_id)

    # --- API keys ----------------------------------------------------------------------------

    def create_api_key(
        self,
        *,
        name: str | None,
        read_only: bool = False,
        expires_days: int | None = None,
        username: str | None = None,
        scope: str | None = None,
        subject_username: str | None = None,
        by: Principal | None = None,
    ) -> CreatedApiKey:
        """`scope`: `read`, `request`, `admin` or `metrics` (default: `read` when `read_only`, else
        `admin`).

        EF-USR-024 (4): the key acts as its **subject** (default: the one who holds it). An adult
        issues `read` or `request` for herself; for a profile it takes an admin. The `admin` scope
        is an admin's own, for herself, never for a subject; `destructive` stays unimplemented."""
        if username is not None:
            owner = self._store.user_by_name(username)
        else:
            admins = self._store.admins()
            owner = admins[0] if admins else None
        if owner is None:
            raise UnknownUserError
        scope = scope or ("read" if read_only else "admin")
        if scope == "request" and subject_username is None:
            raise ValueError("subject_required")  # a request key acts as someone: she is named
        if scope not in KEY_SCOPES:
            raise ValueError("invalid_scope")
        if scope in ("admin", "metrics") and owner.role != "admin":  # an admin's alone
            raise ValueError("scope_requires_admin")
        subject = owner
        if subject_username is not None:
            found = self._store.user_by_name(subject_username)
            if found is None:
                raise UnknownUserError
            subject = found
        if subject.id != owner.id and scope in ("admin", "metrics"):
            raise ValueError("subject_not_allowed")
        if (
            by is not None
            and by.role != "admin"
            and (subject.id != by.user_id or owner.id != by.user_id)
        ):
            raise ValueError("subject_not_allowed")
        read_only = scope in ("read", "metrics")
        now = self.now()
        if expires_days is not None and expires_days < 1:
            raise ValueError("invalid_expiry")
        expires_at = now + timedelta(days=expires_days) if expires_days is not None else None
        if self.hardened:  # P4: optional by default, capped by the hardened profile only
            cap = now + HARDENED_KEY_MAX_AGE
            expires_at = min(expires_at, cap) if expires_at is not None else cap
        secret = new_api_key()
        self._store.insert_api_key(
            NewApiKey(
                id=new_ulid(),
                user_id=owner.id,
                name=name,
                prefix=secret.prefix,
                hash=secret.digest,
                scopes=(scope,),
                read_only=read_only,
                now=now,
                expires_at=expires_at,
                subject_user_id=subject.id,
                # who issued it, when it is an account; NULL for the host operator, whose line
                # in the audit is `local_cli:<uid>` (the column is a foreign key to a user)
                issued_by=by.user_id
                if by is not None and by.credential is Credential.SESSION
                else None,
            )
        )
        self._audit("api_key.created", by, target=_by_host_user(secret.prefix, by))
        return CreatedApiKey(value=secret.value, prefix=secret.prefix, expires_at=expires_at)

    def rotate_api_key(
        self, prefix: str, *, expires_days: int | None = None, by: Principal | None = None
    ) -> CreatedApiKey | None:
        """Replace a live key by a new one with the same owner, name and scope (EF-USR-003, P4).

        The old key dies in the transaction that stores the new one, so a failure leaves it
        working and shows nothing. An expired key may be rotated (that is what the warning before
        its expiry asks for); a revoked or unknown one may not (None). Without `expires_days`
        the new key lives as long as the old one did, or never expires if it never did."""
        if expires_days is not None and expires_days < 1:
            raise ValueError("invalid_expiry")
        old = self._store.api_key_by_prefix(prefix)
        if old is None or old.revoked_at is not None:
            return None
        now = self.now()
        if expires_days is not None:
            expires_at: datetime | None = now + timedelta(days=expires_days)
        elif old.expires_at is not None:
            expires_at = now + (old.expires_at - old.created_at)
        else:
            expires_at = None
        if self.hardened:
            cap = now + HARDENED_KEY_MAX_AGE
            expires_at = min(expires_at, cap) if expires_at is not None else cap
        for _attempt in range(3):  # a taken prefix (1 in 4 billion) is drawn again
            secret = new_api_key()
            fresh = NewApiKey(
                id=new_ulid(),
                user_id=old.user.id,
                name=old.name,
                prefix=secret.prefix,
                hash=secret.digest,
                scopes=old.scopes,
                read_only=old.read_only,
                now=now,
                expires_at=expires_at,
                subject_user_id=old.subject.id if old.subject is not None else None,
                issued_by=old.issued_by,
            )
            audit = AuditEntry(
                actor_id=audit_actor(by),
                action="api_key.rotated",
                at=now,
                target=_by_host_user(f"{prefix}>{secret.prefix}", by),
            )
            try:
                if not self._store.rotate_api_key(prefix, fresh, audit=audit):
                    return None
            except KeyConflictError:
                continue
            return CreatedApiKey(value=secret.value, prefix=secret.prefix, expires_at=expires_at)
        raise RuntimeError("api_key_prefix_exhausted")

    def api_keys(self) -> Sequence[ApiKeyRecord]:
        return self._store.api_keys()

    def revoke_api_key(self, prefix: str, *, by: Principal | None = None) -> bool:
        revoked = self._store.revoke_api_key(prefix, now=self.now())
        if revoked:
            self._audit("api_key.revoked", by, target=_by_host_user(prefix, by))
        return revoked

    def authenticate_api_key(self, value: str) -> Principal | None:
        parsed = parse_api_key(value)
        if parsed is None:
            return None
        record = self._store.api_key_by_prefix(parsed.prefix)
        if record is None or not same_secret(record.hash, parsed.digest):
            return None
        now = self.now()
        if record.revoked_at is not None or (
            record.expires_at is not None and now >= record.expires_at
        ):
            return None
        if record.last_used_at is None or now - record.last_used_at >= API_KEY_TOUCH_INTERVAL:
            self._store.touch_api_key(record.id, now=now)
        # EF-USR-024 (4): every right and every filter is the subject's, never the issuer's
        acts_as = record.subject or record.user
        return Principal(
            user_id=acts_as.id,
            username=acts_as.username,
            role=acts_as.role,
            credential=Credential.API_KEY,
            key_id=record.id,
            scopes=record.scopes,
            read_only=record.read_only,
            can_rate=acts_as.can_rate and "admin" in record.scopes,
            locale=acts_as.locale,
        )

    # --- device tokens (M4 L46, ADR-0053 §4, EF-USR-008) -------------------------------------

    def create_device_token(
        self,
        *,
        subject_username: str,
        name: str | None = None,
        expires_days: int | None = None,
        kind: str | None = None,
        by: Principal | None = None,
    ) -> CreatedDeviceToken:
        """A token for the reader profile `subject_username`, which has no default: the pairing
        asks for the profile (EF-USR-022 (4)). Every right and every filter of the token is the
        subject's; the issuer is kept for the audit and nothing else. An admin or the host issues
        it, nobody else. The secret is generated, returned once and never stored: only the
        SHA-256 of its md5 is."""
        if not isinstance(subject_username, str) or not subject_username.strip():
            raise ValueError("subject_required")
        if by is not None and by.role != "admin":
            raise NotAllowedError
        subject = self._store.user_by_name(subject_username.strip())
        return self._issue_device_token(subject, name, expires_days, by, kind)

    def pair_device_token(
        self,
        *,
        by: Principal,
        subject_user_id: str,
        name: str | None = None,
        expires_days: int | None = None,
        kind: str | None = None,
    ) -> CreatedDeviceToken:
        """The screen "Connect a device" (M4 L46b): an admin pairs for a member or a child, a member
        for themself, a child for nobody. The same rules as above apply to the subject (never an
        admin, so an admin never pairs for themself)."""
        subject = self._store.user_by_id(subject_user_id)
        if not _may_pair(by, subject.id if subject is not None else None):
            raise NotAllowedError  # a member names themself only, a child nobody
        return self._issue_device_token(subject, name, expires_days, by, kind)

    def _issue_device_token(
        self,
        subject: UserRecord | None,
        name: str | None,
        expires_days: int | None,
        by: Principal | None,
        kind: str | None = None,
    ) -> CreatedDeviceToken:
        if subject is None:
            raise UnknownUserError
        try:
            device_type = device_kind(kind)
        except DeviceKindError:
            raise ProfileError("device_kind_unknown") from None
        if subject.role == "admin":  # a lost e-reader would open the whole catalogue, unfiltered
            raise ProfileError("device_subject_is_admin")
        if expires_days is not None and expires_days < 1:
            raise ValueError("invalid_expiry")
        now = self.now()
        expires_at = now + timedelta(days=expires_days) if expires_days is not None else None
        issuer = by.user_id if by is not None and by.credential is Credential.SESSION else None
        label = device_name(name)
        device_id = new_ulid()  # the pairing makes one device; a drawn-again identifier keeps it
        for _attempt in range(3):  # a taken identifier (1 in 10^12) is drawn again
            credential = new_device_credential()
            try:
                self._store.insert_device_token(
                    NewDeviceToken(
                        id=new_ulid(),
                        public_id=credential.public_id,
                        name=label,
                        subject_user_id=subject.id,
                        issued_by=issuer,
                        secret_digest=secret_digest(credential.secret),
                        now=now,
                        expires_at=expires_at,
                        device_id=device_id,
                        kind=device_type,
                    ),
                    self._entry(
                        "device_token.created", by, _by_host_user(credential.public_id, by)
                    ),
                )
            except KeyConflictError:
                continue
            return CreatedDeviceToken(
                public_id=credential.public_id,
                secret=credential.secret,
                subject_user_id=subject.id,
                expires_at=expires_at,
                device_id=device_id,
            )
        raise RuntimeError("device_token_id_exhausted")

    # --- the device registry (M5 L02, EF-USR-008, D19) ----------------------------------------

    def devices(
        self, *, by: Principal | None = None, subject_user_id: str | None = None
    ) -> Sequence[DeviceRecord]:
        """The live devices: every profile's for the host or an admin (narrowed by
        `subject_user_id`), a member's own for a member, and for a child a refusal. A member who
        names another profile is refused as for anything of another's."""
        if by is not None and by.role not in ("admin", "member"):
            raise NotAllowedError
        if by is not None and by.role == "member":
            if subject_user_id is not None and subject_user_id != by.user_id:
                raise NotAllowedError
            subject_user_id = by.user_id
        return tuple(r for r in self._store.devices(subject_user_id) if r.device.active)

    def _own_device(self, device_id: str, by: Principal | None) -> DeviceRecord | None:
        """A live device `by` may handle, or None: unknown, retired or another profile's all
        answer alike (no probing). A child is refused."""
        if by is not None and by.role not in ("admin", "member"):
            raise NotAllowedError
        found = self._store.device_by_id(device_id.strip()[:26]) if device_id else None
        if found is None or not found.device.active:
            return None
        if by is not None and not may_handle(
            role=by.role, user_id=by.user_id, subject_user_id=found.device.subject_user_id
        ):
            return None
        return found

    def rotate_device(
        self, device_id: str, *, by: Principal | None = None, expires_days: int | None = None
    ) -> CreatedDeviceToken | None:
        """A new secret on the same device; the old one is refused from the next request (D19: no
        grace period). The device keeps its identifier, its label, its kind, its profile and its
        public identifier: nothing is paired again, the reader types the new secret. A token
        past its expiry needs a new one (`expiry_required`): the new secret would be refused as
        it is. None: the device is not there (or not the caller's)."""
        found = self._own_device(device_id, by)
        if found is None or found.public_id is None:
            return None
        if expires_days is not None and expires_days < 1:
            raise ValueError("invalid_expiry")
        now = self.now()
        if expires_days is None and found.expires_at is not None and now >= found.expires_at:
            raise ProfileError("expiry_required")
        expires_at = now + timedelta(days=expires_days) if expires_days is not None else None
        credential = new_device_credential()
        outcome = self._store.rotate_device_secret(
            found.device.id,
            expected_digest=found.secret_digest,
            secret_digest=secret_digest(credential.secret),
            expires_at=expires_at,
            now=self.now,
            audit=self._entry("device_token.rotated", by, _by_host_user(found.device.id, by)),
        )
        if outcome == "expired":  # it expired between the read and the write
            raise ProfileError("expiry_required")
        if outcome == "conflict":  # another rotation won: only its secret lives
            raise ProfileError("device_rotated_meanwhile")
        if outcome != "rotated":
            return None
        return CreatedDeviceToken(
            public_id=found.public_id,
            secret=credential.secret,
            subject_user_id=found.device.subject_user_id,
            expires_at=expires_at or found.expires_at,
            device_id=found.device.id,
        )

    def retire_device(self, device_id: str, *, by: Principal | None = None) -> bool:
        """Removing a device ends every token it holds, at once. False: not there (or not the
        caller's), or already retired."""
        found = self._own_device(device_id, by)
        if found is None:
            return False
        return self._store.retire_device(
            found.device.id,
            now=self.now(),
            audit=self._entry("device.removed", by, _by_host_user(found.device.id, by)),
        )

    def device_tokens(self, *, by: Principal | None = None) -> Sequence[DeviceTokenRecord]:
        """Every token for the host or an admin; a member's own for a member; none for a child."""
        everything = self._store.device_tokens()
        if by is None or by.role == "admin":
            return everything
        if by.role != "member":
            return ()
        return tuple(t for t in everything if t.subject.id == by.user_id)

    def revoke_device_token(self, public_id: str, *, by: Principal | None = None) -> bool:
        """Immediate: the next request with it is refused. False: unknown or already revoked, and
        for a member also another profile's token: it answers like one that is not there."""
        if by is not None and by.role != "admin":
            held = self._store.device_token_by_public_id(public_id.strip().lower())
            if by.role != "member" or held is None or held.subject.id != by.user_id:
                return False
        revoked = self._store.revoke_device_token(public_id.strip().lower(), now=self.now())
        if revoked:
            self._audit("device_token.revoked", by, target=_by_host_user(public_id, by))
        return revoked

    def authenticate_device_token(
        self,
        public_id: str,
        *,
        ip: str,
        secret: str | None = None,
        md5_key: str | None = None,
    ) -> Principal | None:
        found = self._device_authenticated(public_id, ip=ip, secret=secret, md5_key=md5_key)
        return found[0] if found is not None else None

    def _device_authenticated(
        self, public_id: str, *, ip: str, secret: str | None, md5_key: str | None
    ) -> tuple[Principal, DeviceTokenRecord] | None:
        """The profile a device token acts as, or None. One of `secret` (HTTP Basic, OPDS) or
        `md5_key` (`x-auth-key` of KOReader, kosync). The answer is the same for an unknown
        identifier and a wrong key. A wrong attempt is counted before the check, per (address,
        identifier); a right one withdraws its count and clears its pair. While a pair is over its
        limit even the right key waits (`ThrottledError`). The address limit counts failures
        alone: past it a wrong guess is a `ThrottledError`, a right secret is still served (with
        130 bits nobody guesses, and a shared address must not lock every e-reader out).
        Rights and filters are read from the subject at each request."""
        wanted = public_id.strip().lower() if isinstance(public_id, str) else ""
        wanted = wanted[: PUBLIC_ID_LENGTH + 8]
        account = f"{address_key(ip)}|{wanted}"
        at = self.now()
        # only the pair is reserved here: the address limit slows failures, never a right secret
        wait = self._device_throttle.reserve(ip=ip, account=account, now=at, skip_address=True)
        if wait is not None:
            raise ThrottledError(max(1, math.ceil(wait)))
        presented = (
            secret_digest(secret)
            if secret is not None and len(secret) <= 256
            else key_digest(md5_key)
        )
        record = (
            self._store.device_token_by_public_id(wanted)
            if len(wanted) == PUBLIC_ID_LENGTH
            else None
        )
        expected = record.secret_digest if record is not None else _NO_TOKEN_DIGEST
        matched = presented is not None and same_secret(expected, presented)
        if record is None or not matched:
            structlog.get_logger().warning("auth.device_token_failed", ip=ip)  # never the key
            self._device_failed(ip, at)
            return None
        now = self.now()
        if not device_token_is_live(
            record.revoked_at, record.device_revoked_at, record.expires_at, now
        ):  # a retired device opens nothing (#347 MIN-2), same rule as an open stream
            self._device_failed(ip, at)  # the guess had the right shape: counted
            return None
        self._device_throttle.release(ip=ip, account=account, at=at, skip_address=True)
        self._device_throttle.reset_account(account)
        return self._device_principal(record, now), record

    def _device_principal(self, record: DeviceTokenRecord, now: datetime) -> Principal:
        if record.last_used_at is None or now - record.last_used_at >= API_KEY_TOUCH_INTERVAL:
            self._store.touch_device_token(record.id, now=now)
        held = device_token(
            subject_user_id=record.subject.id,
            issued_by=record.issued_by or "host",  # the host operator names no account
            token_id=record.id,
        )
        # the subject's role from the database now; a token never rates (EF-USR-024)
        return token_principal(
            held, username=record.subject.username, role=record.subject.role, can_rate=False
        )

    # --- `/lite` (M5 L17a, EF-LEC-010): a browser with no script signs in with a token's secret --

    def lite_sign_in(self, public_id: str, secret: str, *, ip: str) -> str | None:
        """The session cookie of `/lite` for a token and its secret, or None (one answer for an
        unknown identifier, a wrong secret, a revoked, a retired and an expired token). The check
        and its counting are those of the e-reader's own door (`authenticate_device_token`), so
        a guess is slowed the same way and a `ThrottledError` is the same answer."""
        if self._lite is None:
            return None
        found = self._device_authenticated(public_id, ip=ip, secret=secret, md5_key=None)
        if found is None:
            return None
        record = found[1]
        return self._lite.issue(record.public_id, record.secret_digest, self.now())

    def lite_resume(self, value: str | None) -> Principal | None:
        """Who a `/lite` cookie is, at this request: the token is read again (live, its device not
        retired, its secret the one the session was made with) and the profile is the token's
        subject, read now. A rotated secret ends the session; so does anything that ends the
        token. Nothing is counted here: the cookie is signed, there is nothing to guess."""
        if self._lite is None:
            return None
        now = self.now()
        claims = self._lite.verify(value, now)
        if claims is None:
            return None
        record = self._store.device_token_by_public_id(claims.public_id)
        if record is None or not self._lite.bound(claims, record.secret_digest):
            return None
        if not device_token_is_live(
            record.revoked_at, record.device_revoked_at, record.expires_at, now
        ):
            return None
        return self._device_principal(record, now)

    def lite_new_flow(self) -> tuple[str, str]:
        if self._lite is None:
            raise RuntimeError("lite sessions are not configured")
        return self._lite.new_flow()

    def lite_flow_ok(self, nonce: str | None, token: str | None) -> bool:
        return self._lite is not None and self._lite.flow_ok(nonce, token)

    def lite_csrf(self, session: str) -> str:
        if self._lite is None:
            raise RuntimeError("lite sessions are not configured")
        return self._lite.csrf_of(session)

    def lite_csrf_ok(self, session: str | None, token: str | None) -> bool:
        return self._lite is not None and self._lite.csrf_ok(session, token)

    def _device_failed(self, ip: str, at: datetime) -> None:
        """A wrong attempt: counted against the address; past its limit it is a 429, not a 401."""
        wait = self._device_throttle.fail_address(ip=ip, now=at)
        if wait is not None:
            raise ThrottledError(max(1, math.ceil(wait)))

    def authenticate_forward(self, username: str) -> Principal | None:
        """The account a believed proxy named, or None: nobody is created from a header.

        Never fresh (`auth_time` None) and flagged: every reserved action asks for the local
        password again (EF-USR-024 (5)), and an unsafe request must show its own origin."""
        user = self._store.user_by_name(username) if 0 < len(username) <= 256 else None
        if user is None:
            return None
        return Principal(
            user_id=user.id,
            username=user.username,
            role=user.role,
            credential=Credential.SESSION,
            via_forward=True,
            can_rate=user.can_rate,
            locale=user.locale,
        )

    # --- reserved actions --------------------------------------------------------------------

    def restricted_profile_exists(self) -> bool:
        """P3: parental changes need a fresh password only once a restricted profile exists."""
        return self._store.has_role("child")

    def authorize(
        self,
        principal: Principal,
        action: ReservedAction,
        *,
        restricted_profile_exists: bool,
        allowed_roles: frozenset[str] = DEFAULT_RESERVED_ROLES,
        audit: bool = True,
    ) -> Verdict:
        """`audit=False`: the route writes its own line, with its target, in the transaction that
        discloses (the unmasking): the guard then writes none, so one disclosure is one line."""
        verdict = authorize_reserved(
            principal,
            action,
            now=self.now(),
            fresh_auth_s=self.fresh_auth_s,
            restricted_profile_exists=restricted_profile_exists,
            hardened=self.hardened,
            allowed_roles=allowed_roles,
            has_local_factor=not principal.provider_proof or self._has_local_factor(principal),
        )
        if isinstance(verdict, Allowed):  # EF-USR-022 (3): session and auth_time in the audit
            if audit:
                self._audit(action.name, principal)
        else:
            # a reauthentication asked for is the normal way in: INFO; a refusal is a WARNING
            level = "info" if isinstance(verdict, ReauthRequired) else "warning"
            getattr(structlog.get_logger(), level)(
                "auth.reserved_refused",
                action=action.name,
                reason=type(verdict).__name__,
                actor_id=audit_actor(principal),
            )
        return verdict

    def _has_local_factor(self, principal: Principal) -> bool:
        """A password or a live passkey: what the account can prove itself with here. Without
        either the provider is its only authority and its proof stands (docs/admin/oidc.md)."""
        user = self._store.user_by_id(principal.user_id)
        if user is not None and user.password_hash is not None:
            return True
        return any(p.revoked_at is None for p in self._store.passkeys_of(principal.user_id))

    def recent_audit_targets(self, action: str, *, days: int = 30) -> list[str]:
        return self._store.audit_targets(action, since=self.now() - timedelta(days=days))

    def audit_refusal(self, action: str, *, target: str | None = None) -> None:
        self._audit(action, target=target)
