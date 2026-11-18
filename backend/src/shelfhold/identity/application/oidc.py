"""OpenID Connect sign-in and linking (M5 L03, EF-USR-004, EF-USR-024, ADR-0018 amended).

The authorization code flow with PKCE, `state` and `nonce`. What the provider says is one claim
that counts: `sub`, under the `iss` that was configured. A sign-in finds the account linked to
that pair and opens a session on it; it never creates an account, never matches on a name or an
address, and never signs a child profile in. Linking ties an account that already exists to a
person of the provider, from the account's own fresh session or by an admin (a reserved action).

Every refusal at the callback is one answer (`OidcRefusedError`): the person at the browser never
learns whether the `sub` was known, nor which check failed (the reason goes to the log, a code and
never a claim). A flow is single use, bound to the browser that began it by a cookie only that
browser holds, and lives ten minutes in the memory of the one process (a restart asks the person
to start again, as for a passkey).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import structlog

from shelfhold.identity.application.ports import IdentityStore, OidcLink
from shelfhold.identity.application.service import (
    InvalidCredentialsError,
    IssuedSession,
    LocalAuth,
)
from shelfhold.identity.domain.throttle import address_key

FLOW_TTL = timedelta(minutes=10)
FLOW_COOKIE_MAX_AGE_S = int(FLOW_TTL.total_seconds())
# "No local proof of when the person signed in": a session opened by the provider is as old as the
# epoch, so no reserved action that asks for a recent proof is open to it. The provider's own
# `auth_time` is not trusted for that here: nothing asked it to sign the person in again
# (`prompt=login`, `max_age`), so a compromised provider account would open every fresh action.
# L04 asks, verifies the claim, and only then makes a session fresh.
NEVER = datetime(1970, 1, 1, tzinfo=UTC)

log = structlog.get_logger()

Purpose = Literal["login", "link"]


class OidcError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OidcUnavailableError(OidcError):
    """The provider cannot be asked (not reachable, refused by the guard, or not what it says)."""


class OidcRefusedError(OidcError):
    """The sign-in or the link is refused: one answer whatever the reason. `purpose` is set only
    once the browser has shown it is the one that began the flow (else it is not told anything)."""

    def __init__(self, reason: str, purpose: Purpose | None = None) -> None:
        super().__init__(reason)
        self.purpose = purpose


@dataclass(frozen=True)
class OidcClaims:
    """What a verified ID token is reduced to: nothing else of it is read or kept."""

    issuer: str
    sub: str
    auth_time: datetime | None


class OidcProvider(Protocol):
    """The provider, as the application needs it: the library and the network are behind it."""

    issuer: str

    def pkce(self) -> tuple[str, str]:
        """A verifier and its S256 challenge."""
        ...

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        """Raises `OidcUnavailableError`."""
        ...

    def complete(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str, now: datetime
    ) -> OidcClaims:
        """Exchanges the code and verifies the ID token. Raises `OidcRefusedError` when the answer
        is not acceptable and `OidcUnavailableError` when the provider cannot be asked."""
        ...


@dataclass(frozen=True)
class OidcFlow:
    purpose: Purpose
    nonce: str
    verifier: str
    binder_digest: str  # of the cookie value only the starting browser holds
    expires_at: datetime
    user_id: str | None = None  # a link: the account it is for
    session_id: str | None = None  # a link: the session that started it, which must still live


class OidcFlows(Protocol):
    def issue(self, state: str, flow: OidcFlow, *, owner: str, now: datetime) -> None: ...
    def consume(self, state: str) -> OidcFlow | None:
        """Removed whatever the outcome: a state is used once."""
        ...


@dataclass(frozen=True)
class OidcStart:
    url: str
    binder: str  # the value of the flow cookie


@dataclass(frozen=True)
class OidcOutcome:
    purpose: Purpose
    issued: IssuedSession | None = None  # a sign-in: the new session


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OidcAuth:
    def __init__(
        self,
        *,
        auth: LocalAuth,
        store: IdentityStore,
        provider: OidcProvider,
        flows: OidcFlows,
        redirect_uri: str,
    ) -> None:
        self._auth = auth
        self._store = store
        self._provider = provider
        self._flows = flows
        self._redirect_uri = redirect_uri

    @property
    def issuer(self) -> str:
        return self._provider.issuer

    def _begin(
        self,
        purpose: Purpose,
        *,
        owner: str,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> OidcStart:
        now = self._auth.now()
        state, nonce, binder = (secrets.token_urlsafe(32) for _ in range(3))
        verifier, challenge = self._provider.pkce()
        url = self._provider.authorization_url(
            redirect_uri=self._redirect_uri, state=state, nonce=nonce, code_challenge=challenge
        )
        self._flows.issue(
            state,
            OidcFlow(
                purpose, nonce, verifier, _digest(binder), now + FLOW_TTL, user_id, session_id
            ),
            owner=owner,
            now=now,
        )
        return OidcStart(url, binder)

    def begin_login(self, *, ip: str) -> OidcStart:
        """Anonymous and cheap to call: each address gets a few a minute (`ThrottledError`)."""
        self._auth.reserve_options(ip=ip)
        return self._begin("login", owner=address_key(ip))  # the limiter's own unit (a /64)

    def begin_link(self, *, user_id: str, session_id: str) -> OidcStart:
        """For the account of a session the caller has already shown to be fresh and an adult's."""
        return self._begin("link", owner=f"user:{user_id}", user_id=user_id, session_id=session_id)

    def finish(
        self,
        *,
        state: str,
        code: str,
        binder: str,
        ip: str,
        user_agent: str | None = None,
        presented_token: str | None = None,
    ) -> OidcOutcome:
        """Raises `ThrottledError` (counted before anything is checked, like a password),
        `OidcRefusedError` and `OidcUnavailableError`."""
        account = f"oidc:{ip}"  # no account is known before the token: one key per address
        at = self._auth.reserve_attempt(ip=ip, account=account)
        flow = self._flows.consume(state)  # used up whatever follows
        now = self._auth.now()
        if flow is None or now >= flow.expires_at:
            raise self._refuse("unknown_or_expired_state", ip)
        if not secrets.compare_digest(flow.binder_digest, _digest(binder)):
            raise self._refuse("other_browser", ip)
        if not code:  # the provider sent the browser back with an error (or with nothing)
            raise self._refuse("no_code", ip, flow.purpose)
        try:
            claims = self._provider.complete(
                code=code,
                redirect_uri=self._redirect_uri,
                code_verifier=flow.verifier,
                nonce=flow.nonce,
                now=now,
            )
        except OidcRefusedError as refused:
            raise self._refuse(refused.reason, ip, flow.purpose) from None
        except OidcUnavailableError as down:  # nothing to tell the browser but the refusal
            raise self._refuse(f"provider_{down.reason}", ip, flow.purpose) from None
        if claims.issuer != self._provider.issuer:  # the adapter checks it; belt and braces
            raise self._refuse("issuer", ip, flow.purpose)
        outcome = (
            self._finish_login(claims, ip=ip, user_agent=user_agent, presented=presented_token)
            if flow.purpose == "login"
            else self._finish_link(flow, claims, ip=ip)
        )
        self._auth.release_attempt(ip=ip, account=account, at=at)
        return outcome

    def _refuse(self, reason: str, ip: str, purpose: Purpose | None = None) -> OidcRefusedError:
        log.warning("auth.oidc_refused", reason=reason, ip=ip)  # never a claim, never a code
        return OidcRefusedError(reason, purpose)

    def _finish_login(
        self, claims: OidcClaims, *, ip: str, user_agent: str | None, presented: str | None
    ) -> OidcOutcome:
        user = self._store.oidc_user(claims.issuer, claims.sub)
        if user is None:
            raise self._refuse("no_linked_account", ip, "login")  # no account is ever created here
        if user.role == "child":
            raise self._refuse("child_profile", ip, "login")
        try:
            issued = self._auth.open_federated_session(
                user,
                ip=ip,
                user_agent=user_agent,
                presented_token=presented,
                auth_time=NEVER,
                oidc=(claims.issuer, claims.sub),
            )
        except InvalidCredentialsError:  # unlinked or made a child since it was read
            raise self._refuse("link_withdrawn", ip, "login") from None
        return OidcOutcome("login", issued)

    def _finish_link(self, flow: OidcFlow, claims: OidcClaims, *, ip: str) -> OidcOutcome:
        if flow.user_id is None or flow.session_id is None:
            raise self._refuse("link_without_account", ip, "link")
        if not self._store.session_is_live(flow.session_id, now=self._auth.now()):
            raise self._refuse("session_gone", ip, "link")  # closed since the link was asked for
        outcome = self._auth.link_oidc_identity(
            user_id=flow.user_id, issuer=claims.issuer, sub=claims.sub, session_id=flow.session_id
        )
        if outcome not in (OidcLink.LINKED, OidcLink.UNCHANGED):
            raise self._refuse(f"link_{outcome.value}", ip, "link")
        return OidcOutcome("link")


__all__ = [
    "FLOW_COOKIE_MAX_AGE_S",
    "NEVER",
    "OidcAuth",
    "OidcClaims",
    "OidcError",
    "OidcFlow",
    "OidcFlows",
    "OidcOutcome",
    "OidcProvider",
    "OidcRefusedError",
    "OidcStart",
    "OidcUnavailableError",
]
