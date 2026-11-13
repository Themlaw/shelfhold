"""Who is calling, and for how long a session lives (EF-USR-022, EF-USR-024, ADR-0049).

Arbitrage P2: one default for every role, 30 days sliding, "remember me" ticked. The short admin
durations (30 min idle, 12 h absolute) belong to the hardened profile only.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class Credential(StrEnum):
    """The credential axis of the truth table (EF-USR-024). Freshness is a property of a session,
    not a credential of its own: `auth_time` says how recent the last password entry is."""

    SESSION = "session"
    API_KEY = "api_key"
    DEVICE_TOKEN = "device_token"  # noqa: S105 (a credential kind, not a secret). M5; refused like a key wherever an action is reserved
    LOCAL_CLI = "local_cli"  # only the Unix socket server (L23) or the offline CLI assigns it


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: str
    credential: Credential
    session_id: str | None = None
    auth_time: datetime | None = None  # last password entry; sessions only
    key_id: str | None = None  # API key or device token
    scopes: tuple[str, ...] = ()
    read_only: bool = False
    can_rate: bool = False  # the right to classify (EF-BIB-036); closed unless the user holds it
    via_forward: bool = (
        False  # named by a believed proxy: never fresh, no reserved action (EF-USR-024)
    )

    @property
    def actor_id(self) -> str:
        """`<credential>:<id>`: one session, one key or one device token, never a whole user
        (review PR #41): two sessions of one account never share an idempotency key."""
        match self.credential:
            case Credential.SESSION if self.via_forward:
                return f"forward:{self.user_id}"
            case Credential.SESSION:
                return f"session:{self.session_id}"
            case Credential.API_KEY | Credential.DEVICE_TOKEN:
                return f"{self.credential.value}:{self.key_id}"
            case _:
                return f"{self.credential.value}:{self.user_id}"

    @property
    def is_host(self) -> bool:
        """The host CLI over its socket (not a session, a key or a token)."""
        return self.credential is Credential.LOCAL_CLI

    @property
    def may_write(self) -> bool:
        if self.read_only:
            return False
        if self.credential is Credential.API_KEY:
            return bool({"admin", "request"} & set(self.scopes))
        return True


# The `credential` axis of the truth table (EF-USR-024, ADR-0049): what the registry of reserved
# actions is checked against, cell by cell. Keys are told apart by scope.
CREDENTIAL_AXIS = (
    "fresh_session",
    "session",
    "api_key:read",
    "api_key:request",
    "api_key:admin",
    "device_token",
    "local_cli",
)


def credential_axis(principal: Principal, *, now: datetime, fresh_auth_s: int) -> str:
    """The label of a principal on the axis. A key holds one scope in practice; the widest wins."""
    match principal.credential:
        case Credential.SESSION:
            fresh = (
                not principal.via_forward
                and principal.auth_time is not None
                and (0 <= (now - principal.auth_time).total_seconds() <= fresh_auth_s)
            )
            return "fresh_session" if fresh else "session"
        case Credential.API_KEY:
            for scope in ("admin", "request", "read"):
                if scope in principal.scopes:
                    return f"api_key:{scope}"
            return "api_key:read"  # no scope known: the narrowest label
        case Credential.DEVICE_TOKEN:
            return "device_token"
        case Credential.LOCAL_CLI:
            return "local_cli"


@dataclass(frozen=True)
class SessionPolicy:
    idle: timedelta
    absolute: timedelta | None  # None: the expiry slides with every request
    remember_default: bool = True


DEFAULT_POLICY = SessionPolicy(idle=timedelta(days=30), absolute=None)
HARDENED_ADMIN_POLICY = SessionPolicy(
    idle=timedelta(minutes=30), absolute=timedelta(hours=12), remember_default=False
)  # EF-USR-022: "remember me" is refused until the admin confirms it
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)  # `last_seen_at` is written at most this often
HARDENED_KEY_MAX_AGE = timedelta(days=90)
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024  # bounds the argon2 input


def session_policy(role: str, *, hardened: bool) -> SessionPolicy:
    if hardened and role == "admin":
        return HARDENED_ADMIN_POLICY
    return DEFAULT_POLICY


@dataclass(frozen=True)
class SessionTimes:
    created_at: datetime
    last_seen_at: datetime
    absolute_expiry: datetime


def session_expired(times: SessionTimes, policy: SessionPolicy, now: datetime) -> bool:
    """The stored expiry, the idle limit and, when the policy has one, the absolute cap (which
    also binds sessions opened before the hardened profile was switched on)."""
    if now >= times.absolute_expiry or now - times.last_seen_at >= policy.idle:
        return True
    return policy.absolute is not None and now >= times.created_at + policy.absolute


def initial_expiry(policy: SessionPolicy, now: datetime) -> datetime:
    return now + (policy.absolute if policy.absolute is not None else policy.idle)


def slid_expiry(times: SessionTimes, policy: SessionPolicy, now: datetime) -> datetime:
    """A sliding session moves its expiry with each touch; an absolute one never does."""
    if policy.absolute is not None:
        return min(times.absolute_expiry, times.created_at + policy.absolute)
    return now + policy.idle


def check_password_shape(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("password_too_short")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("password_too_long")
