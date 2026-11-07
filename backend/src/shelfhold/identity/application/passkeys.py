"""The passkey ceremonies (EF-USR-025): options, then verification, one step at a time.

This module decides nothing about sessions or the login throttle (`LocalAuth` does); it keeps
the ceremony honest:
- the challenge is drawn here, bound to a purpose and to the session (or the flow), consumed once
  and expiring (`MemoryChallenges`);
- the relying party comes from the public URL only;
- an assertion is checked in this order: the flow, the credential (known, live, the right
  account), the library (signature, challenge, origin, RP ID, user verification), and only then
  the counter, so that nobody without a valid signature can make us write a counter alert;
- nothing here logs a credential id, a key, a handle or a challenge: refusals carry a closed
  reason word.
"""

import hashlib
import secrets
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

import structlog

from shelfhold.identity.application.ports import (
    AuditEntry,
    IdentityStore,
    NewPasskey,
    PasskeyRecord,
    UserRecord,
)
from shelfhold.identity.domain.passkeys import (
    MAX_PASSKEYS,
    PasskeysUnavailable,
    Purpose,
    RelyingParty,
    VerifiedCredential,
    WebAuthnRefusedError,
    clean_name,
    counter_regressed,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid


class WebAuthnPort(Protocol):
    def registration_options(
        self,
        *,
        rp: RelyingParty,
        user_handle: bytes,
        username: str,
        challenge: bytes,
        exclude: Sequence[bytes],
    ) -> dict[str, Any]: ...
    def authentication_options(
        self, *, rp: RelyingParty, challenge: bytes, allow: Sequence[bytes]
    ) -> dict[str, Any]: ...
    def verify_registration(
        self, *, rp: RelyingParty, credential: Mapping[str, object], challenge: bytes
    ) -> VerifiedCredential: ...
    def credential_id(self, credential: Mapping[str, object]) -> bytes: ...
    def user_handle(self, credential: Mapping[str, object]) -> bytes | None: ...
    def verify_assertion(
        self,
        *,
        rp: RelyingParty,
        credential: Mapping[str, object],
        challenge: bytes,
        public_key: bytes,
    ) -> int: ...


class Issued(Protocol):
    @property
    def flow(self) -> str: ...
    @property
    def challenge(self) -> bytes: ...


class ChallengePort(Protocol):
    def issue(self, purpose: Purpose, *, bound_to: str, owner: str, now: datetime) -> Issued: ...
    def consume(
        self, flow: str, purpose: Purpose, *, bound_to: str, now: datetime
    ) -> bytes | None: ...


class PasskeyError(Exception):
    """Base of the refusals; `reason` is a closed word, safe for a log line."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PasskeyUnavailableError(PasskeyError):
    """No secure context, or no public URL: the function does not exist."""


class PasskeyRefusedError(PasskeyError):
    pass


class PasskeyOptions:
    def __init__(self, flow: str, options: dict[str, Any], binder: str | None = None) -> None:
        self.flow = flow
        self.options = options
        self.binder = binder  # sign-in only: the secret the browser keeps in its flow cookie


def _digest(binder: str) -> str:
    return hashlib.sha256(binder.encode()).hexdigest()


class PasskeyCeremony:
    def __init__(
        self,
        store: IdentityStore,
        webauthn: WebAuthnPort,
        challenges: ChallengePort,
        rp: RelyingParty | PasskeysUnavailable,
        clock: Clock,
        *,
        anonymous_challenges: ChallengePort | None = None,
    ) -> None:
        self._store = store
        self._webauthn = webauthn
        self._challenges = challenges  # registration and re-authentication: tied to a session
        # Sign-in challenges have no session yet: a store of their own, so that a flood of
        # anonymous requests can never push out the challenge of someone who is registering.
        self._anonymous = anonymous_challenges or challenges
        self._rp = rp
        self._clock = clock

    # --- availability ------------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return isinstance(self._rp, RelyingParty)

    @property
    def rp_id(self) -> str | None:
        return self._rp.id if isinstance(self._rp, RelyingParty) else None

    @property
    def unavailable_reason(self) -> str | None:
        return self._rp.reason if isinstance(self._rp, PasskeysUnavailable) else None

    def _require(self) -> RelyingParty:
        if not isinstance(self._rp, RelyingParty):
            raise PasskeyUnavailableError(self._rp.reason)
        return self._rp

    def _now(self) -> datetime:
        return self._clock.now()

    # --- registration ------------------------------------------------------------------------

    def registration_options(self, user: UserRecord, session_id: str) -> PasskeyOptions:
        rp = self._require()
        live = self._store.passkeys_of(user.id)
        if len(live) >= MAX_PASSKEYS:
            raise PasskeyRefusedError("too_many")
        issued = self._challenges.issue(
            "registration", bound_to=session_id, owner=session_id, now=self._now()
        )
        options = self._webauthn.registration_options(
            rp=rp,
            user_handle=user.id.encode(),
            username=user.username,
            challenge=issued.challenge,
            exclude=[p.credential_id for p in live],
        )
        return PasskeyOptions(issued.flow, options)

    def finish_registration(
        self,
        user: UserRecord,
        session_id: str,
        flow: str,
        credential: Mapping[str, object],
        name: str,
    ) -> PasskeyRecord:
        rp = self._require()
        challenge = self._challenges.consume(
            flow, "registration", bound_to=session_id, now=self._now()
        )
        if challenge is None:
            raise PasskeyRefusedError("challenge")
        cleaned = clean_name(name)
        if cleaned is None:
            raise PasskeyRefusedError("name")
        if len(self._store.passkeys_of(user.id)) >= MAX_PASSKEYS:
            raise PasskeyRefusedError("too_many")
        try:
            verified = self._webauthn.verify_registration(
                rp=rp, credential=credential, challenge=challenge
            )
        except WebAuthnRefusedError as error:
            raise PasskeyRefusedError(str(error) or "verification") from None
        new = NewPasskey(
            id=new_ulid(),
            user_id=user.id,
            credential_id=verified.credential_id,
            public_key=verified.public_key,
            sign_count=verified.sign_count,
            transports=verified.transports,
            aaguid=verified.aaguid,
            name=cleaned,
            now=self._now(),
            rp_id=rp.id,
        )
        if not self._store.insert_passkey(new):
            raise PasskeyRefusedError("duplicate")
        stored = self._store.passkey_by_credential(verified.credential_id)
        if stored is None:  # pragma: no cover (just inserted)
            raise PasskeyRefusedError("duplicate")
        return stored

    # --- assertions (login, re-authentication) -----------------------------------------------

    def login_options(self) -> PasskeyOptions:
        """The flow is bound to the browser that asked: a random `binder` it keeps in a cookie
        (only its digest is held here), not to an address that a household or a proxy shares."""
        rp = self._require()
        binder = secrets.token_urlsafe(32)
        issued = self._anonymous.issue(
            "login", bound_to=_digest(binder), owner=_digest(binder), now=self._now()
        )
        # No `allowCredentials`: the authenticator offers its discoverable credentials.
        return PasskeyOptions(
            issued.flow,
            self._webauthn.authentication_options(rp=rp, challenge=issued.challenge, allow=()),
            binder,
        )

    def reauth_options(self, user: UserRecord, session_id: str) -> PasskeyOptions:
        rp = self._require()
        issued = self._challenges.issue(
            "reauth", bound_to=session_id, owner=session_id, now=self._now()
        )
        allow = [p.credential_id for p in self._store.passkeys_of(user.id)]
        return PasskeyOptions(
            issued.flow,
            self._webauthn.authentication_options(rp=rp, challenge=issued.challenge, allow=allow),
        )

    def finish_login(
        self, flow: str, binder: str, credential: Mapping[str, object]
    ) -> tuple[UserRecord, PasskeyRecord]:
        return self._assert(flow, "login", _digest(binder), credential, expected_user=None)

    def finish_reauth(
        self, user: UserRecord, session_id: str, flow: str, credential: Mapping[str, object]
    ) -> PasskeyRecord:
        return self._assert(flow, "reauth", session_id, credential, expected_user=user)[1]

    def _assert(
        self,
        flow: str,
        purpose: Purpose,
        bound_to: str,
        credential: Mapping[str, object],
        *,
        expected_user: UserRecord | None,
    ) -> tuple[UserRecord, PasskeyRecord]:
        rp = self._require()
        now = self._now()
        bank = self._anonymous if purpose == "login" else self._challenges
        challenge = bank.consume(flow, purpose, bound_to=bound_to, now=now)
        if challenge is None:
            raise PasskeyRefusedError("challenge")  # unknown, replayed, expired, or not ours
        try:
            credential_id = self._webauthn.credential_id(credential)
            handle = self._webauthn.user_handle(credential)
        except WebAuthnRefusedError as error:
            raise PasskeyRefusedError(str(error) or "malformed") from None
        passkey = self._store.passkey_by_credential(credential_id)
        if passkey is None:
            raise PasskeyRefusedError("unknown_credential")
        if passkey.revoked_at is not None:
            raise PasskeyRefusedError("revoked")
        if expected_user is not None and passkey.user_id != expected_user.id:
            raise PasskeyRefusedError("foreign_credential")
        user = expected_user or self._store.user_by_id(passkey.user_id)
        if user is None:
            raise PasskeyRefusedError("unknown_user")
        if handle is not None and handle != user.id.encode():
            raise PasskeyRefusedError("user_handle")
        try:
            presented = self._webauthn.verify_assertion(
                rp=rp, credential=credential, challenge=challenge, public_key=passkey.public_key
            )
        except WebAuthnRefusedError as error:
            raise PasskeyRefusedError(str(error) or "verification") from None
        if counter_regressed(passkey.sign_count, presented) or not self._store.record_passkey_use(
            passkey.id, sign_count=presented, now=now
        ):
            self._store.audit(
                AuditEntry(
                    actor_id=user.id,
                    action="auth.passkey_counter_regression",
                    at=now,
                    target=passkey.id,
                )
            )
            structlog.get_logger().warning("auth.passkey_counter_regression", user_id=user.id)
            raise PasskeyRefusedError("counter")
        return user, passkey
