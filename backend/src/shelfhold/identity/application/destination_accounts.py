"""A member's account on a destination: their own Kavita key, sealed (EF-USR-012, D8).

Each member pastes the key of their own Kavita account. It is sealed by the `SecretVault`, per user
and per destination, and nothing reads it back: the API shows a fingerprint, the admin never sees
a key, and the only reader is `open_token`, for the progress synchronisation of that member (L14).

Who may do what, decided here (the HTTP guard says it first, a session only and never a key):

- an adult (admin or member) links, rotates and revokes **their own** accounts;
- a child links nothing: the parent (an admin) enters the key of a child, as a reserved action,
  for the account the parent declared in the profile (`kavita_username`, EF-USR-017);
- the admin revokes any account and reads none; they do not link an adult's;
- the profile of another answers like one that is not there: nothing tells them apart (no probing).

At link time the key must open the account that was declared: the verifier (the Kavita adapter,
through the guarded fetcher) asks Kavita who the key is. Nothing is kept for a key that did not
verify. Audit lines name the user and the destination, never a key and never an account name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Protocol

from shelfhold.identity.application.ports import AuditEntry
from shelfhold.identity.domain.credentials import Credential, Principal
from shelfhold.identity.domain.kavita_mapping import check_kavita_username, same_kavita_username
from shelfhold.identity.domain.roles import ProfileError

__all__ = [
    "AccountChoice",
    "AccountRefused",
    "AccountStore",
    "AccountVerifier",
    "AccountView",
    "DestinationAccountError",
    "DestinationAccounts",
    "DestinationRef",
    "RemoteAccount",
    "SecretToken",
    "SyncStatusRow",
    "UserFacts",
]

MAX_TOKEN: Final = 512
MIN_TOKEN: Final = 8  # what the vault registers as a secret; a shorter one is no Kavita key
ADULTS: Final = frozenset({"admin", "member"})

ERRORS: Final = frozenset(
    {
        "not_allowed", "not_found", "remote_user_taken", "remote_user_required",
        "invalid_remote_user", "invalid_token", "child_account_not_declared",
        "child_account_mismatch", "token_refused", "destination_not_supported",
        "destination_unreachable", "child_account_admin", "child_account_privileged",
        "child_account_unrestricted", "child_account_restriction_too_open",
    }
)  # fmt: skip


class DestinationAccountError(Exception):
    """A refusal; `code` is one of `ERRORS`. The message is the code, never a key or a name."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AccountRefused(Exception):
    """What a verifier says when the key is not accepted (`token_refused`,
    `destination_unreachable`, `destination_not_supported`, and for a child `child_account_admin`,
    `child_account_privileged`, `child_account_unrestricted`,
    `child_account_restriction_too_open`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SecretToken:
    """An opened key. It does not print; `reveal` is the one way to the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretToken(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class AccountView:
    """What the API may show of an account: never the key, only its fingerprint."""

    id: str
    user_id: str
    shelf_destination_id: str
    remote_user: str | None  # None once revoked
    token_fingerprint: str | None
    verified_at: datetime | None
    revoked_at: datetime | None
    warnings: tuple[str, ...] = ()  # said once, when the key is linked; never part of a list


@dataclass(frozen=True)
class SyncStatusRow:
    """One member on one Kavita binding: is a key linked, and when did the sync last pull and push.
    Nothing of the key, of its fingerprint or of the Kavita account."""

    user_id: str
    username: str
    shelf_destination_id: str
    shelf_name: str
    target_name: str
    state: str  # `linked` or `no_token`
    last_pull_at: datetime | None
    last_push_at: datetime | None


@dataclass(frozen=True)
class AccountChoice:
    """A binding a profile may link a key to: where it is (shelf, server) and whether a key is
    linked. Nothing of the account, nothing of the key."""

    id: str
    shelf_name: str
    target_name: str
    linked: bool


@dataclass(frozen=True)
class UserFacts:
    id: str
    role: str
    kavita_username: str | None
    max_age_rating: int | None = None  # the ceiling of a child


@dataclass(frozen=True)
class DestinationRef:
    id: str
    supported: bool  # a binding to a Kavita
    allowed_profiles: tuple[str, ...] = ()  # the shelf's own list; empty: open to everyone
    shelf_name: str = ""
    target_name: str = ""

    def open_to(self, user_id: str) -> bool:
        return not self.allowed_profiles or user_id in self.allowed_profiles


@dataclass(frozen=True)
class RemoteAccount:
    """What the verifier learned of the account a key opens, and nothing else."""

    is_admin: bool


class AccountVerifier(Protocol):
    def destination(self, destination_id: str) -> DestinationRef | None: ...

    def kavita_destinations(self) -> list[DestinationRef]:
        """Every binding to a Kavita, named: the choices of a member."""
        ...

    def verify(
        self, destination_id: str, remote_user: str, token: str, *, child_max_age: int | None
    ) -> RemoteAccount:
        """Returns when `token` opens the account `remote_user`, raises `AccountRefused`. With
        `child_max_age` (the ceiling of a child) the account must also be one a child may hold:
        not an administrator, no right beyond reading and progress, a restriction at or below
        the ceiling."""
        ...


class AccountStore(Protocol):
    def user(self, user_id: str) -> UserFacts | None: ...

    def members(self) -> list[tuple[str, str]]:
        """Every profile, `(id, username)`, in a stable order."""
        ...

    def sync_cursors(self) -> dict[tuple[str, str], tuple[datetime | None, datetime | None]]:
        """`(user_id, destination_id)` -> (last pull, last push) of the progress sync."""
        ...

    def accounts_of(self, user_id: str) -> list[AccountView]: ...

    def save(
        self,
        user_id: str,
        destination_id: str,
        remote_user: str,
        token: str,
        *,
        declared: str | None,
        now: datetime,
        entry: Callable[[bool], AuditEntry],
    ) -> bool:
        """Seal and keep the key in one transaction with its audit line. `declared` is the name a
        child's parent declared, re-read under the lock that writes. Returns True when an earlier
        key was replaced."""
        ...

    def revoke(
        self, user_id: str, destination_id: str, *, now: datetime, entry: AuditEntryFactory
    ) -> bool: ...

    def open(self, user_id: str, destination_id: str) -> str | None: ...


AuditEntryFactory = Callable[[], AuditEntry]


def _check_token(token: object) -> str:
    if not isinstance(token, str) or not MIN_TOKEN <= len(token) <= MAX_TOKEN:
        raise DestinationAccountError("invalid_token")
    if token != token.strip() or any(not c.isprintable() or c.isspace() for c in token):
        raise DestinationAccountError("invalid_token")
    return token


class DestinationAccounts:
    def __init__(
        self,
        store: AccountStore,
        verifier: AccountVerifier,
        *,
        now: Callable[[], datetime],
        audit_entry: Callable[[str, Principal | None, str | None], AuditEntry],
    ) -> None:
        self._store = store
        self._verifier = verifier
        self._now = now
        self._entry = audit_entry

    def __repr__(self) -> str:
        return "DestinationAccounts()"

    # --- who may act ------------------------------

    @staticmethod
    def _adult_session(principal: Principal) -> None:
        if principal.credential is not Credential.SESSION or principal.role not in ADULTS:
            raise DestinationAccountError("not_allowed")

    def _target(
        self, principal: Principal, user_id: str, *, child_only_for_others: bool
    ) -> UserFacts:
        """The profile the call is about, or `not_found` for another's (and for one that is not
        there): a member sees no difference. An admin reaches a child's, and with
        `child_only_for_others=False` (revoke, list) any profile."""
        own = user_id == principal.user_id
        if not own and (principal.role != "admin"):
            raise DestinationAccountError("not_found")
        facts = self._store.user(user_id)
        if facts is None:
            raise DestinationAccountError("not_found")
        if not own and child_only_for_others and facts.role != "child":
            raise DestinationAccountError("not_found")
        return facts

    # --- link, rotate ------------------------------

    def bind(
        self,
        principal: Principal,
        user_id: str,
        destination_id: str,
        *,
        token: object,
        remote_user: object = None,
    ) -> AccountView:
        self._adult_session(principal)
        target = self._target(principal, user_id, child_only_for_others=True)
        destination = self._verifier.destination(destination_id)
        if destination is None:
            raise DestinationAccountError("not_found")
        if target.role != "admin" and not destination.open_to(target.id):
            raise DestinationAccountError("not_found")  # a shelf closed to them: as if not there
        if not destination.supported:
            raise DestinationAccountError("destination_not_supported")
        declared: str | None = None
        if target.role == "child":
            declared = target.kavita_username
            if declared is None:
                raise DestinationAccountError("child_account_not_declared")
            if remote_user is not None and not (
                isinstance(remote_user, str) and same_kavita_username(declared, remote_user)
            ):
                raise DestinationAccountError("child_account_mismatch")
            name = declared
        else:
            if remote_user is None:
                raise DestinationAccountError("remote_user_required")
            try:
                checked = check_kavita_username(remote_user)
            except ProfileError:
                raise DestinationAccountError("invalid_remote_user") from None
            if checked is None:
                raise DestinationAccountError("remote_user_required")
            name = checked
        secret = _check_token(token)
        try:
            remote = self._verifier.verify(
                destination_id, name, secret,
                child_max_age=target.max_age_rating if target.role == "child" else None,
            )  # fmt: skip
        except AccountRefused as refused:
            raise DestinationAccountError(refused.code) from None
        self._store.save(
            user_id, destination_id, name, secret, declared=declared, now=self._now(),
            entry=lambda replaced: self._entry(
                "destination_account.rotated" if replaced else "destination_account.linked",
                principal, f"{user_id}|{destination_id}",
            ),
        )  # fmt: skip
        for view in self._store.accounts_of(user_id):
            if view.shelf_destination_id == destination_id:
                # an adult's key of an administrator account: allowed, said once (a child's never)
                return replace(view, warnings=("kavita_admin",) if remote.is_admin else ())
        raise DestinationAccountError("not_found")  # pragma: no cover  (saved a moment ago)

    # --- read, revoke ------------------------------

    def list_accounts(self, principal: Principal, user_id: str) -> list[AccountView]:
        self._adult_session(principal)
        self._target(principal, user_id, child_only_for_others=False)
        return self._store.accounts_of(user_id)

    def choices(self, principal: Principal, user_id: str) -> list[AccountChoice]:
        """The Kavita bindings the profile may link a key to: those of the shelves open to them
        (an admin's own: all). For a child, the parent's view of the child's choices."""
        self._adult_session(principal)
        target = self._target(principal, user_id, child_only_for_others=False)
        linked = {
            a.shelf_destination_id for a in self._store.accounts_of(user_id) if a.revoked_at is None
        }
        return [
            AccountChoice(d.id, d.shelf_name, d.target_name, d.id in linked)
            for d in self._verifier.kavita_destinations()
            if d.supported and (target.role == "admin" or d.open_to(target.id))
        ]

    def sync_status(self, principal: Principal) -> list[SyncStatusRow]:
        """The state of the progress sync, member by member (EF-EXP-026). An admin reads every
        member's; anyone else, their own and nothing else (no id is taken: there is no door to
        another's). A child reads nothing, like their key screen."""
        self._adult_session(principal)
        everyone = principal.role == "admin"
        members = [m for m in self._store.members() if everyone or m[0] == principal.user_id]
        destinations = [d for d in self._verifier.kavita_destinations() if d.supported]
        cursors = self._store.sync_cursors()
        rows: list[SyncStatusRow] = []
        for user_id, username in members:
            live = {
                a.shelf_destination_id
                for a in self._store.accounts_of(user_id)
                if a.revoked_at is None
            }
            for d in destinations:
                if not d.open_to(user_id):
                    continue
                pull, push = (
                    cursors.get((user_id, d.id), (None, None)) if d.id in live else (None, None)
                )
                rows.append(
                    SyncStatusRow(
                        user_id, username, d.id, d.shelf_name, d.target_name,
                        "linked" if d.id in live else "no_token", pull, push,
                    )
                )  # fmt: skip
        return rows

    def revoke(self, principal: Principal, user_id: str, destination_id: str) -> None:
        self._adult_session(principal)
        self._target(principal, user_id, child_only_for_others=False)
        done = self._store.revoke(
            user_id, destination_id, now=self._now(),
            entry=lambda: self._entry(
                "destination_account.revoked", principal, f"{user_id}|{destination_id}"
            ),
        )  # fmt: skip
        if not done:
            raise DestinationAccountError("not_found")

    # --- the one reader of a key (the progress synchronisation, L14) ------------------------------

    def open_token(self, user_id: str, destination_id: str) -> SecretToken | None:
        value = self._store.open(user_id, destination_id)
        return None if value is None else SecretToken(value)
