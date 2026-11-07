"""Passkeys (EF-USR-025, ADR-0050): the rules that need neither a library nor a database.

- the relying party (RP ID and origin) is derived from `SHELFHOLD_PUBLIC_URL` and nothing else:
  a request's `Host` header says nothing about who the server is (ENF-SEC-016);
- the function exists only in a secure context: HTTPS, or `http://localhost`; and the RP ID must
  be a domain name (WebAuthn refuses an IP address);
- a challenge is random, bound to one purpose and one session or flow, used once, and expires;
- the signature counter must advance, except for an authenticator that never counts (a constant
  0, ADR-0050: synced passkeys).
"""

import ipaddress
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit

MAX_PASSKEYS = 25  # per account
MAX_NAME = 64
CHALLENGE_BYTES = 32
CHALLENGE_TTL = timedelta(minutes=2)  # the browser is asked for 60 s
MAX_CREDENTIAL_BYTES = 32_768  # a registration or assertion is a few hundred bytes
KNOWN_TRANSPORTS = frozenset({"usb", "nfc", "ble", "smart-card", "hybrid", "internal"})

Purpose = Literal["registration", "login", "reauth"]
Reason = Literal["no_public_url", "invalid_url", "insecure_context", "ip_host"]
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class RelyingParty:
    id: str  # the host name, e.g. `shelf.example.org`
    origin: str  # `https://shelf.example.org:8443`: the only origin accepted, compared as a string


@dataclass(frozen=True)
class PasskeysUnavailable:
    reason: Reason


@dataclass(frozen=True)
class VerifiedCredential:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    aaguid: str | None
    transports: tuple[str, ...]


class WebAuthnRefusedError(Exception):
    """The library (or one of our own checks) refused a ceremony message."""


def relying_party(public_url: str | None) -> RelyingParty | PasskeysUnavailable:
    if not public_url:
        return PasskeysUnavailable("no_public_url")
    try:
        parts = urlsplit(public_url.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return PasskeysUnavailable("invalid_url")
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not host or parts.username or parts.password:
        return PasskeysUnavailable("invalid_url")
    host = host.lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return PasskeysUnavailable("ip_host")  # not a domain: no RP ID, whatever the scheme
    if scheme == "http" and host != "localhost":
        return PasskeysUnavailable("insecure_context")
    shown = f":{port}" if port is not None and port != _DEFAULT_PORTS[scheme] else ""
    return RelyingParty(id=host, origin=f"{scheme}://{host}{shown}")


def counter_regressed(stored: int, presented: int) -> bool:
    """A counter must move forward. An authenticator that never counts stays at 0 (tolerated);
    once either side is above 0, a value that does not exceed the stored one is a clone or a
    replay."""
    return (stored > 0 or presented > 0) and presented <= stored


def clean_name(name: str) -> str | None:
    cleaned = name.strip()
    return cleaned if 0 < len(cleaned) <= MAX_NAME else None
