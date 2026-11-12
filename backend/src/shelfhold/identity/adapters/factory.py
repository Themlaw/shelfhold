"""Composition of the local authentication over one SQLite engine."""

from collections.abc import Callable

from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.identity.adapters.challenges import MemoryChallenges
from shelfhold.identity.adapters.device_cookie import DeviceCookieSigner
from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.identity.adapters.store import SqlIdentityStore
from shelfhold.identity.application.passkeys import PasskeyCeremony
from shelfhold.identity.application.ports import PasskeyRecord
from shelfhold.identity.application.service import LocalAuth
from shelfhold.identity.domain.passkeys import relying_party
from shelfhold.identity.webauthn.library import WebAuthnLibrary
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.identity_counter import IdentityCounter

ANONYMOUS_CAPACITY = 4096  # sign-in challenges in flight, a store apart from the sessions' own


def local_auth(
    engine: Engine,
    settings: Settings,
    clock: Clock | None = None,
    *,
    on_passkey_registered: Callable[[PasskeyRecord], None] | None = None,
) -> LocalAuth:
    store = SqlIdentityStore(engine, IdentityCounter(settings.data_dir))
    clock = clock or SystemClock()
    # The relying party is the public URL and nothing else (ADR-0050); without a secure context
    # the ceremony stays, but every step refuses and the login page offers nothing.
    passkeys = PasskeyCeremony(
        store,
        WebAuthnLibrary(),
        MemoryChallenges(),
        relying_party(settings.public_url),
        clock,
        anonymous_challenges=MemoryChallenges(capacity=ANONYMOUS_CAPACITY),
    )
    return LocalAuth(
        store,
        Argon2Hasher(),
        clock,
        passkeys=passkeys,
        hardened=settings.auth_hardened,
        fresh_auth_s=settings.fresh_auth_s,
        device_proof=DeviceCookieSigner(settings.data_dir),
        on_passkey_registered=on_passkey_registered,
    )
