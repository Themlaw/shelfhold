"""Opaque bearer secrets: session tokens, CSRF secrets and API keys (EF-USR-003, EF-USR-021).

Tokens carry 256 random bits, so a fast digest is enough to store them: the database holds
`sha256:<hex>`, never the token. API keys read `shk_<prefix>_<secret>`; the prefix is public
(listing, revocation), the secret is shown once.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

API_KEY_PREFIX = "shk"
_API_KEY = re.compile(r"shk_([0-9a-f]{8})_([A-Za-z0-9_-]{43})")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def same_secret(left: str, right: str) -> bool:
    """Constant-time comparison of two strings (CSRF tokens, digests)."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass(frozen=True)
class ApiKeySecret:
    prefix: str
    value: str  # the whole `shk_…` string, shown once

    @property
    def digest(self) -> str:
        return token_digest(self.value)


def new_api_key() -> ApiKeySecret:
    prefix = secrets.token_hex(4)
    return ApiKeySecret(
        prefix=prefix, value=f"{API_KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
    )


def parse_api_key(value: str) -> ApiKeySecret | None:
    match = _API_KEY.fullmatch(value)
    return ApiKeySecret(prefix=match.group(1), value=value) if match else None
