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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import structlog

from shelfhold.identity.application.ports import (
    ApiKeyRecord,
    AuditEntry,
    DeviceProofPort,
    IdentityStore,
    KeyConflictError,
    NewApiKey,
    NewSession,
    PasswordHasherPort,
    ReauthMethod,
    UserRecord,
)
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
from shelfhold.identity.domain.reserved import (
    DEFAULT_FRESH_AUTH_S,
    DEFAULT_RESERVED_ROLES,
    Allowed,
    ReservedAction,
    Verdict,
    authorize_reserved,
)
from shelfhold.identity.domain.throttle import LoginThrottle
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
IDENTITY_HEADER_AUDIT = "auth.identity_header_stripped"  # read back by `shelfhold status`
API_KEY_TOUCH_INTERVAL = timedelta(minutes=1)


class AuthError(Exception):
    """Base of the expected refusals; the message is a stable code."""


class InvalidCredentialsError(AuthError):
    def __init__(self) -> None:
        super().__init__("invalid_credentials")


class ThrottledError(AuthError):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__("too_many_attempts")
        self.retry_after_s = retry_after_s


class AdminExistsError(AuthError):
    def __init__(self) -> None:
        super().__init__("admin_exists")


class UnknownUserError(AuthError):
    def __init__(self) -> None:
        super().__init__("unknown_user")


class UnknownReauthMethodError(AuthError):
    def __init__(self) -> None:
        super().__init__("unknown_reauth_method")


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
class CreatedApiKey:
    value: str  # shown once
    prefix: str
    expires_at: datetime | None


def _by_host_user(target: str, by: Principal | None = None) -> str:
    """Offline, the CLI has no principal: the audit keeps at least the uid of the host user who
    ran it (review PR #43, M6). Through the local socket (L23) the principal carries the uid and
    the audit actor is `local_cli:<uid>`, so the target stays clean."""
    return target if by is not None else f"{target} uid={os.getuid()}"


def _audit_actor(principal: Principal | None) -> str | None:
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
    ) -> None:
        self._store = store
        self._hasher = hasher
        self._clock = clock
        self.hardened = hardened
        self.fresh_auth_s = fresh_auth_s
        self._throttle = throttle or LoginThrottle()
        self._device_proof = device_proof
        methods = reauth_methods if reauth_methods is not None else [PasswordReauth(hasher)]
        self._reauth = {method.name: method for method in methods}

    @property
    def reauth_method_names(self) -> tuple[str, ...]:
        return tuple(self._reauth)

    def now(self) -> datetime:
        return self._clock.now()

    def _policy(self, role: str) -> SessionPolicy:
        return session_policy(role, hardened=self.hardened)

    def _audit(
        self, action: str, principal: Principal | None = None, *, target: str | None = None
    ) -> None:
        """Offline host CLI actions have no principal; through the local socket the principal is
        `local_cli` and the actor is `local_cli:<uid>` (L23)."""
        entry = AuditEntry(
            actor_id=_audit_actor(principal),
            action=action,
            at=self.now(),
            target=target,
            session_id=principal.session_id if principal else None,
            auth_time=principal.auth_time if principal else None,
        )
        self._store.audit(entry)

    # --- accounts (CLI) ----------------------------------------------------------------------

    def create_admin(self, username: str, password: str, *, by: Principal | None = None) -> str:
        username = username.strip()
        if not username or len(username) > 128:
            raise ValueError("invalid_username")
        check_password_shape(password)
        if self._store.admins():
            raise AdminExistsError  # one local admin in M1a (EF-USR-021)
        user_id = new_ulid()
        self._store.insert_user(
            id=user_id,
            username=username,
            role=ADMIN_ROLE,
            password_hash=self._hasher.hash(password),
            now=self.now(),
        )
        self._audit("user.created", by, target=user_id)
        return user_id

    def reset_password(self, username: str, password: str, *, by: Principal | None = None) -> int:
        """New password, and every session of the account closed. Returns the closed count."""
        check_password_shape(password)
        user = self._store.user_by_name(username)
        if user is None:
            raise UnknownUserError
        self._store.set_password_hash(user.id, self._hasher.hash(password))
        closed = self._store.revoke_sessions(now=self.now(), user_id=user.id)
        self._audit("user.password_reset", by, target=user.id)
        return closed

    # --- sessions ----------------------------------------------------------------------------

    def _issue(self, user: UserRecord, *, remember: bool, ip: str | None) -> IssuedSession:
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
            )
        )
        principal = Principal(
            user_id=user.id,
            username=user.username,
            role=user.role,
            credential=Credential.SESSION,
            session_id=session_id,
            auth_time=now,
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
        presented_token: str | None = None,
        device_proof: str | None = None,
    ) -> IssuedSession:
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
        if self._hasher.needs_rehash(user.password_hash):
            self._store.set_password_hash(user.id, self._hasher.hash(password))
        if presented_token:  # whatever the browser held is closed, never adopted
            self._store.revoke_session_by_token_hash(token_digest(presented_token), now=self.now())
        issued = self._issue(user, remember=remember, ip=ip)
        self._audit("auth.login", issued.principal)
        if self._device_proof is not None:
            issued = replace(issued, device_proof=self._device_proof.issue(username, self.now()))
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
        if user is None or not verifier.verify(user, payload):
            raise self._fail(ip=ip)
        self._throttle.release(ip=ip, account=principal.username, at=at)
        now = self.now()
        token, csrf = new_token(), new_token()
        self._store.rotate_session(
            principal.session_id, token_hash=token_digest(token), csrf_secret=csrf, now=now
        )
        renewed = replace(principal, auth_time=now)
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

    # --- API keys ----------------------------------------------------------------------------

    def create_api_key(
        self,
        *,
        name: str | None,
        read_only: bool = False,
        expires_days: int | None = None,
        username: str | None = None,
        by: Principal | None = None,
    ) -> CreatedApiKey:
        if username is not None:
            owner = self._store.user_by_name(username)
        else:
            admins = self._store.admins()
            owner = admins[0] if admins else None
        if owner is None:
            raise UnknownUserError
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
                scopes=("read",) if read_only else ("admin",),
                read_only=read_only,
                now=now,
                expires_at=expires_at,
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
            )
            audit = AuditEntry(
                actor_id=_audit_actor(by),
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
        return Principal(
            user_id=record.user.id,
            username=record.user.username,
            role=record.user.role,
            credential=Credential.API_KEY,
            key_id=record.id,
            scopes=record.scopes,
            read_only=record.read_only,
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
    ) -> Verdict:
        verdict = authorize_reserved(
            principal,
            action,
            now=self.now(),
            fresh_auth_s=self.fresh_auth_s,
            restricted_profile_exists=restricted_profile_exists,
            hardened=self.hardened,
            allowed_roles=allowed_roles,
        )
        if isinstance(verdict, Allowed):  # EF-USR-022 (3): session and auth_time in the audit
            self._audit(action.name, principal)
        return verdict

    def recent_audit_targets(self, action: str, *, days: int = 30) -> list[str]:
        return self._store.audit_targets(action, since=self.now() - timedelta(days=days))

    def audit_refusal(self, action: str, *, target: str | None = None) -> None:
        self._audit(action, target=target)
