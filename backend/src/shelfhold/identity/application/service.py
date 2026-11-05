"""Local authentication: admin account, server sessions, API keys, re-authentication.

Invariants this module keeps (EF-USR-021, EF-USR-022, EF-USR-003, ENF-SEC-008):
- the database holds digests of tokens and keys, never the tokens or keys themselves;
- a login always opens a new session with a new token, and closes the one it was sent with
  (no fixation); a re-authentication renews the token and the CSRF secret;
- an unknown user and a wrong password cost the same argon2 verification and give the same error;
- a throttled attempt is refused before any password is checked.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import structlog

from shelfhold.identity.application.ports import (
    ApiKeyRecord,
    AuditEntry,
    IdentityStore,
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
    ) -> None:
        self._store = store
        self._hasher = hasher
        self._clock = clock
        self.hardened = hardened
        self.fresh_auth_s = fresh_auth_s
        self._throttle = throttle or LoginThrottle()
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
        """Host CLI actions have no principal yet (the local socket arrives with L23)."""
        entry = AuditEntry(
            actor_id=principal.user_id if principal else None,
            action=action,
            at=self.now(),
            target=target,
            session_id=principal.session_id if principal else None,
            auth_time=principal.auth_time if principal else None,
        )
        self._store.audit(entry)

    # --- accounts (CLI) ----------------------------------------------------------------------

    def create_admin(self, username: str, password: str) -> str:
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
        self._audit("user.created", target=user_id)
        return user_id

    def reset_password(self, username: str, password: str) -> int:
        """New password, and every session of the account closed. Returns the closed count."""
        check_password_shape(password)
        user = self._store.user_by_name(username)
        if user is None:
            raise UnknownUserError
        self._store.set_password_hash(user.id, self._hasher.hash(password))
        closed = self._store.revoke_sessions(now=self.now(), user_id=user.id)
        self._audit("user.password_reset", target=user.id)
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

    def _check_throttle(self, *, ip: str, account: str) -> None:
        wait = self._throttle.retry_after(ip=ip, account=account, now=self.now())
        if wait is not None:
            raise ThrottledError(max(1, math.ceil(wait)))

    def _fail(self, *, ip: str, account: str) -> InvalidCredentialsError:
        self._throttle.failure(ip=ip, account=account, now=self.now())
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
    ) -> IssuedSession:
        self._check_throttle(ip=ip, account=username)
        user = self._store.user_by_name(username)
        # Same argon2 cost whether the user exists, has no password, or typed it wrong.
        valid = self._hasher.verify(user.password_hash if user else None, password)
        if user is None or user.password_hash is None or not valid:
            raise self._fail(ip=ip, account=username)
        if self._hasher.needs_rehash(user.password_hash):
            self._store.set_password_hash(user.id, self._hasher.hash(password))
        if presented_token:  # whatever the browser held is closed, never adopted
            self._store.revoke_session_by_token_hash(token_digest(presented_token), now=self.now())
        issued = self._issue(user, remember=remember, ip=ip)
        self._audit("auth.login", issued.principal)
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

    def revoke_sessions(self, *, username: str | None = None, session_id: str | None = None) -> int:
        user_id = None
        if username is not None:
            user = self._store.user_by_name(username)
            if user is None:
                raise UnknownUserError
            user_id = user.id
        closed = self._store.revoke_sessions(now=self.now(), user_id=user_id, session_id=session_id)
        self._audit("auth.sessions_revoked", target=session_id or user_id or "*")
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
        self._check_throttle(ip=ip, account=principal.username)
        user = self._store.user_by_id(principal.user_id)
        if user is None or not verifier.verify(user, payload):
            raise self._fail(ip=ip, account=principal.username)
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
        self._audit("api_key.created", target=secret.prefix)
        return CreatedApiKey(value=secret.value, prefix=secret.prefix, expires_at=expires_at)

    def api_keys(self) -> Sequence[ApiKeyRecord]:
        return self._store.api_keys()

    def revoke_api_key(self, prefix: str) -> bool:
        revoked = self._store.revoke_api_key(prefix, now=self.now())
        if revoked:
            self._audit("api_key.revoked", target=prefix)
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
        self, principal: Principal, action: ReservedAction, *, restricted_profile_exists: bool
    ) -> Verdict:
        verdict = authorize_reserved(
            principal,
            action,
            now=self.now(),
            fresh_auth_s=self.fresh_auth_s,
            restricted_profile_exists=restricted_profile_exists,
            hardened=self.hardened,
        )
        if isinstance(verdict, Allowed):  # EF-USR-022 (3): session and auth_time in the audit
            self._audit(action.name, principal)
        return verdict

    def audit_refusal(self, action: str, *, target: str | None = None) -> None:
        self._audit(action, target=target)
