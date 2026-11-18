"""An OpenID Connect provider in process (M5 L03, EF-USR-004): discovery, keys and a token
endpoint behind an `httpx2` transport, so that nothing leaves the machine.

The keys are generated when the provider is built and the client secret is drawn at random: no
secret is written in a test. The provider enforces what a real one enforces (a single-use code,
the PKCE verifier against the challenge, the redirect address and the client of the code), so that
a client that skips one of them fails against it. `tamper` bends one claim of the next ID token
(`aud`, `iss`, `nonce`, `exp`...), `sign_with_other_key` signs it with a key the keys document does
not hold. `e2e/fakes/oidc/serve.py` lends it a socket for the end-to-end run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from joserfc import jwt
from joserfc.jwk import KeySet, OctKey, RSAKey

from tests.fakes_fetcher import FakeResolver

PUBLIC_IP = "93.184.216.34"
ISSUER = "https://idp.example.test"
CLIENT_ID = "shelfhold-test-client"
_KEYS: dict[str, RSAKey] = {}


def _key(kid: str) -> RSAKey:
    if kid not in _KEYS:  # an RSA key takes a moment to draw: once per process and name
        _KEYS[kid] = RSAKey.generate_key(2048, {"kid": kid, "use": "sig", "alg": "RS256"})
    return _KEYS[kid]


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class FakeOidcProvider:
    def __init__(self, *, issuer: str = ISSUER, client_id: str = CLIENT_ID) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.client_secret = secrets.token_urlsafe(24)
        self.host = urlsplit(issuer).hostname or "idp.example.test"
        self.sub = "subject-" + secrets.token_hex(4)
        self.resolver = FakeResolver({self.host: [PUBLIC_IP]})
        self.requests: list[httpx2.Request] = []
        self.tamper: dict[str, Any] = {}
        self.sign_with_other_key = False
        self.public_client = False  # no secret expected (PKCE alone)
        self.publish_shared_secret = False  # the client secret listed among the keys, as `oct`
        self.discovery_overrides: dict[str, Any] = {}
        self.now = time.time  # replaced by a test that moves its own clock
        self.auth_time_age_s: int | None = 0
        self._codes: dict[str, dict[str, Any]] = {}

    # --- what the host reads --------------------------------------------------------------------

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def discovery(self) -> dict[str, Any]:
        document = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        }
        return {**document, **self.discovery_overrides}

    # --- what the browser does at the provider ---------------------------------------------------

    def authorize(self, authorization_url: str, *, sub: str | None = None) -> str:
        """The address the provider sends the browser back to once the person signed in there."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        assert urlsplit(authorization_url).geturl().startswith(f"{self.issuer}/authorize")
        assert query["response_type"] == "code"
        assert query["client_id"] == self.client_id
        assert query["code_challenge_method"] == "S256", "PKCE is not optional"
        assert "openid" in query["scope"].split()
        assert query["state"] and query["nonce"] and query["code_challenge"]
        code = secrets.token_urlsafe(24)
        self._codes[code] = {
            "challenge": query["code_challenge"],
            "nonce": query["nonce"],
            "redirect_uri": query["redirect_uri"],
            "sub": sub or self.sub,
            "client_id": query["client_id"],
        }
        return f"{query['redirect_uri']}?code={code}&state={query['state']}"

    def claims(self, grant: Mapping[str, Any]) -> dict[str, Any]:
        now = int(self.now())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": grant["sub"],
            "aud": self.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": grant["nonce"],
        }
        if self.auth_time_age_s is not None:
            claims["auth_time"] = now - self.auth_time_age_s
        claims.update({k: v for k, v in self.tamper.items() if not k.startswith("__")})
        return {k: v for k, v in claims.items() if v is not None}

    def id_token(self, grant: Mapping[str, Any]) -> str:
        if self.tamper.get("__alg") == "HS256":  # signed with the client secret: key confusion
            shared = OctKey.import_key(self.client_secret, {"kid": "shared"})
            return jwt.encode({"alg": "HS256", "kid": "shared"}, self.claims(grant), shared)
        if self.tamper.get("__alg") == "none":  # an unsigned token: `alg: none`, empty signature
            part = [{"alg": "none", "typ": "JWT"}, self.claims(grant)]
            enc = [
                base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode() for p in part
            ]
            return ".".join(enc) + "."
        key = _key("unpublished") if self.sign_with_other_key else _key("signing-1")
        # the header always names the published key: the signature is what differs
        return jwt.encode({"alg": "RS256", "kid": "signing-1"}, self.claims(grant), key)

    # --- the wire -------------------------------------------------------------------------------

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/.well-known/openid-configuration":
            return httpx2.Response(200, json=self.discovery())
        if request.method == "GET" and path == "/jwks":
            keys: list[Any] = [_key("signing-1")]
            if self.publish_shared_secret:  # a provider (or an attacker) that lists a secret key
                keys.append(OctKey.import_key(self.client_secret, {"kid": "shared", "use": "sig"}))
            return httpx2.Response(200, json=KeySet(keys).as_dict(private=False))
        if request.method == "POST" and path == "/token":
            return self._token(request)
        return httpx2.Response(404, json={"error": "not_found"})

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        failure = httpx2.Response(400, json={"error": "invalid_grant"})
        grant = self._codes.pop(form.get("code", ""), None)  # single use, whatever follows
        if grant is None:
            return failure
        presented = form.get("client_secret")
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Basic "):
            _, _, presented = base64.b64decode(authorization[6:]).decode().partition(":")
        if self.public_client:
            presented = self.client_secret if presented is None else presented
        if presented != self.client_secret:  # a confidential client must prove it
            return httpx2.Response(401, json={"error": "invalid_client"})
        verifier = form.get("code_verifier", "")
        if (
            not verifier
            or s256(verifier) != grant["challenge"]
            or form.get("redirect_uri") != grant["redirect_uri"]
            or form.get("grant_type") != "authorization_code"
        ):
            return failure
        body = {"token_type": "Bearer", "access_token": secrets.token_urlsafe(16)}
        body["id_token"] = self.id_token(grant)
        return httpx2.Response(
            200, content=json.dumps(body), headers={"content-type": "application/json"}
        )
