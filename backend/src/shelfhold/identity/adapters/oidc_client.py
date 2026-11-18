"""The OpenID Connect client (M5 L03, EF-USR-004, ENF-SEC-001): Authlib and its JOSE library behind
the Fetcher.

Authlib draws the PKCE verifier and computes its S256 challenge; its JOSE library (`joserfc`, the
same author) checks the signature. The network is none of theirs: discovery, the token endpoint
and the keys are read through the host's Fetcher and its SSRF guard (a declared LAN provider is
the administrator's own, like a Kavita; every other address must be public, and a redirect is
never followed). Authlib and `joserfc` are imported here and nowhere else (contract
`authlib-confined`).

The ID token is accepted only when all of this holds, and a failure is one refusal with a reason
that goes to the log and never to the browser:
- the signature is one of the provider's published keys, with an asymmetric algorithm from a closed
  list (never `none`, never a shared-secret one);
- `iss` is the issuer the provider's own discovery document states, and that document names the
  issuer that was configured (a provider that names another one is refused: mix-up);
- `aud` is this client and nobody else (a list with another audience is refused);
- `exp` is not past and `iat` not in the future (a minute of leeway for the clocks), `sub` is a
  plain non-empty string, and `nonce` is the one this flow sent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import structlog
from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from shelfhold.identity.application.oidc import (
    OidcClaims,
    OidcRefusedError,
    OidcUnavailableError,
)
from shelfhold.infrastructure.fetcher import FetchError, FetchLimits, GuardedFetcher, Network

log = structlog.get_logger()

LEEWAY = timedelta(seconds=60)
DISCOVERY_TTL = timedelta(minutes=10)
MAX_BYTES = 256 * 1024  # a discovery document, a key set, a token response: never a page
TOTAL_TIMEOUT_S = 15.0
VERIFIER_LENGTH = 64  # RFC 7636: 43 to 128
# Asymmetric only: a symmetric one would have the client's own secret sign what it then trusts
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
MAX_SUB_LENGTH = 255  # OpenID Connect Core, 2


@dataclass(frozen=True)
class _Discovery:
    issuer: str  # as the provider states it (an exact match for `iss`)
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    basic_auth: bool  # `client_secret_basic` is accepted (else the secret goes in the body)
    expires_at: datetime


def _https(url: Any) -> str:
    if not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
        raise OidcUnavailableError("endpoint_not_https")
    return url


def _object(body: bytes) -> dict[str, Any]:
    try:
        loaded = json.loads(body)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


class AuthlibOidcProvider:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        network: Network,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret or None
        self._network = network
        self._discovery: _Discovery | None = None
        self._lock = threading.Lock()

    # --- what the application asks -------------------------------------------------------------

    def pkce(self) -> tuple[str, str]:
        verifier = generate_token(VERIFIER_LENGTH)
        return verifier, create_s256_code_challenge(verifier)

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        found = self._discover()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            quote_via=quote,
        )
        separator = "&" if "?" in found.authorization_endpoint else "?"
        return f"{found.authorization_endpoint}{separator}{query}"

    def complete(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str, now: datetime
    ) -> OidcClaims:
        found = self._discover()
        try:
            token, keys = asyncio.run(self._exchange(found, code, redirect_uri, code_verifier))
        except FetchError as error:
            raise OidcUnavailableError(error.code) from None
        return self._verify(token, keys, found, nonce=nonce, now=now)

    # --- the network, through the Fetcher ------------------------------------------------------

    async def _fetcher(self) -> GuardedFetcher:
        limits = FetchLimits(max_bytes=MAX_BYTES, total_timeout=TOTAL_TIMEOUT_S, max_redirects=0)
        return await self._network.open_integration_fetcher(self.issuer, limits)

    def _discover(self) -> _Discovery:
        with self._lock:
            now = datetime.now(UTC)
            if self._discovery is not None and now < self._discovery.expires_at:
                return self._discovery
            try:
                found = asyncio.run(self._read_discovery(now))
            except FetchError as error:
                raise OidcUnavailableError(error.code) from None
            self._discovery = found
            return found

    async def _read_discovery(self, now: datetime) -> _Discovery:
        fetcher = await self._fetcher()
        try:
            response = await fetcher.get(f"{self.issuer}/.well-known/openid-configuration")
        finally:
            await fetcher.aclose()
        document = _object(response.body)
        if response.status != 200 or not document:
            raise OidcUnavailableError("discovery_failed")
        stated = document.get("issuer")
        # the document must name the issuer that was configured (a trailing slash aside)
        if not isinstance(stated, str) or stated.rstrip("/") != self.issuer:
            raise OidcUnavailableError("discovery_names_another_issuer")
        methods = document.get("token_endpoint_auth_methods_supported")
        basic = not isinstance(methods, list) or "client_secret_basic" in methods
        return _Discovery(
            issuer=stated,
            authorization_endpoint=_https(document.get("authorization_endpoint")),
            token_endpoint=_https(document.get("token_endpoint")),
            jwks_uri=_https(document.get("jwks_uri")),
            basic_auth=basic,
            expires_at=now + DISCOVERY_TTL,
        )

    async def _exchange(
        self, found: _Discovery, code: str, redirect_uri: str, verifier: str
    ) -> tuple[str, KeySet]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        headers = {"Accept": "application/json"}
        if self._client_secret is None:
            form["client_id"] = self._client_id  # a public client: PKCE alone
        elif found.basic_auth:
            pair = f"{quote(self._client_id, safe='')}:{quote(self._client_secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:
            form["client_id"] = self._client_id
            form["client_secret"] = self._client_secret
        fetcher = await self._fetcher()
        try:
            answer = await fetcher.post(found.token_endpoint, data=form, headers=headers)
            body = _object(answer.body)
            token = body.get("id_token")
            if answer.status != 200 or not isinstance(token, str) or not token:
                raise OidcRefusedError("token_endpoint_refused")
            keys = await fetcher.get(found.jwks_uri)
        finally:
            await fetcher.aclose()
        document = _object(keys.body)
        if keys.status != 200 or not isinstance(document.get("keys"), list):
            raise OidcUnavailableError("keys_unavailable")
        try:
            return token, KeySet.import_key_set(document)
        except (JoseError, ValueError, KeyError, TypeError):
            raise OidcUnavailableError("keys_unreadable") from None

    # --- the ID token --------------------------------------------------------------------------

    def _verify(
        self, token: str, keys: KeySet, found: _Discovery, *, nonce: str, now: datetime
    ) -> OidcClaims:
        try:
            decoded = jwt.decode(token, keys, algorithms=ALGORITHMS)
        except (JoseError, ValueError, KeyError, TypeError):
            raise OidcRefusedError("signature_or_format") from None
        claims: Mapping[str, Any] = decoded.claims
        if claims.get("iss") != found.issuer:
            raise OidcRefusedError("issuer")
        audience = claims.get("aud")
        if audience != self._client_id and audience != [self._client_id]:
            raise OidcRefusedError("audience")  # this client and nobody else
        moment = now.timestamp()
        exp, iat = claims.get("exp"), claims.get("iat")
        if not _number(exp) or moment >= exp + LEEWAY.total_seconds():
            raise OidcRefusedError("expired")
        if not _number(iat) or iat > moment + LEEWAY.total_seconds():
            raise OidcRefusedError("issued_in_the_future")
        sent = claims.get("nonce")
        if not isinstance(sent, str) or not secrets.compare_digest(sent, nonce):
            raise OidcRefusedError("nonce")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > MAX_SUB_LENGTH:
            raise OidcRefusedError("sub")
        stated = claims.get("auth_time")
        auth_time = datetime.fromtimestamp(stated, UTC) if _number(stated) and stated > 0 else None
        return OidcClaims(issuer=self.issuer, sub=sub, auth_time=auth_time)


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
