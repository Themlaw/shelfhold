"""Composition of the local authentication over one SQLite engine."""

from collections.abc import Callable

from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.identity.adapters.challenges import MemoryChallenges
from shelfhold.identity.adapters.device_cookie import DeviceCookieSigner, LiteCookieSigner
from shelfhold.identity.adapters.oidc_client import AuthlibOidcProvider
from shelfhold.identity.adapters.oidc_flows import MemoryOidcFlows
from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.identity.adapters.store import SqlIdentityStore
from shelfhold.identity.application.oidc import OidcAuth
from shelfhold.identity.application.passkeys import PasskeyCeremony
from shelfhold.identity.application.ports import PasskeyRecord
from shelfhold.identity.application.service import LocalAuth
from shelfhold.identity.domain.passkeys import relying_party
from shelfhold.identity.webauthn.library import WebAuthnLibrary
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.fetcher import Network
from shelfhold.infrastructure.identity_counter import IdentityCounter

OIDC_CALLBACK_PATH = "/api/v1/auth/oidc/callback"  # the route is `interfaces.http_api.auth_oidc`
ANONYMOUS_CAPACITY = 4096  # sign-in challenges in flight, a store apart from the sessions' own


def local_auth(
    engine: Engine,
    settings: Settings,
    clock: Clock | None = None,
    *,
    on_passkey_registered: Callable[[PasskeyRecord], None] | None = None,
    network: Network | None = None,
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
    auth = LocalAuth(
        store,
        Argon2Hasher(),
        clock,
        passkeys=passkeys,
        hardened=settings.auth_hardened,
        fresh_auth_s=settings.fresh_auth_s,
        device_proof=DeviceCookieSigner(settings.data_dir),
        lite_session=LiteCookieSigner(settings.data_dir),
        on_passkey_registered=on_passkey_registered,
    )
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_client_id:
        # EF-USR-004: the provider is asked through the host's Fetcher (SSRF guard); without the
        # variables nothing of it exists: no route answers, the login page shows no link
        secret = settings.oidc_client_secret
        auth.oidc = OidcAuth(
            auth=auth,
            store=store,
            provider=AuthlibOidcProvider(
                issuer=settings.oidc_issuer,
                client_id=settings.oidc_client_id,
                client_secret=secret.get_secret_value() if secret else None,
                network=network or Network(),
            ),
            flows=MemoryOidcFlows(),
            redirect_uri=f"{(settings.public_url or '').rstrip('/')}{OIDC_CALLBACK_PATH}",
        )
    return auth
