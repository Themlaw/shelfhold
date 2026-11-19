"""The ticket of `/content` (EF-LEC-005, ENF-SEC-004, plan M5 D11): one opaque word that a reader
carries in the path of a request for the bytes of a book.

It is signed (HMAC-SHA-256, a key of the instance) and short-lived (ten minutes at most). It is
bound to the profile, to the carrier, to a scope (`full`, or `excerpt` with the digest of the closed
list of entries the server holds) and to a fingerprint of the session that asked for it: the
session itself is never in it. It grants nothing by itself: the server looks again at what the
profile may see on every request (`ContentAccess`). Pure: no clock, no key, no database here.
"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

TICKET_TTL_S: Final = 600  # D11: ten minutes, renewed by the reader
Scope = Literal["full", "excerpt"]
_TAG_BYTES = 16
_MAX_LENGTH = 1024


@dataclass(frozen=True)
class TicketClaims:
    user_id: str
    carrier_type: str
    carrier_id: str
    scope: Scope
    entries: str | None  # sha256 of the closed list of entries, for `excerpt`; None for `full`
    session_fp: str
    expires_at: int  # epoch seconds


def session_fingerprint(key: bytes, session_id: str) -> str:
    return hmac.new(key, b"session:" + session_id.encode(), hashlib.sha256).hexdigest()[:32]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _tag(key: bytes, body: str) -> str:
    return _b64(hmac.new(key, b"ticket:" + body.encode(), hashlib.sha256).digest()[:_TAG_BYTES])


def seal(key: bytes, claims: TicketClaims) -> str:
    payload = [
        1,
        claims.user_id,
        claims.carrier_type,
        claims.carrier_id,
        claims.scope,
        claims.entries,
        claims.session_fp,
        claims.expires_at,
    ]
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_tag(key, body)}"


def open_ticket(key: bytes, token: str, *, now: datetime) -> TicketClaims | None:
    """The claims of a ticket that this key signed and that has not expired, else None. A forged,
    mangled or expired ticket is one answer: nothing says which."""
    if not token or len(token) > _MAX_LENGTH or token.count(".") != 1:
        return None
    body, _, tag = token.partition(".")
    if not hmac.compare_digest(tag.encode(), _tag(key, body).encode()):
        return None
    try:
        version, user_id, carrier_type, carrier_id, scope, entries, fingerprint, expires = (
            json.loads(_unb64(body))
        )
    except (ValueError, TypeError):
        return None
    if version != 1 or scope not in ("full", "excerpt") or not isinstance(expires, int):
        return None
    if not all(isinstance(v, str) for v in (user_id, carrier_type, carrier_id, fingerprint)):
        return None
    if entries is not None and not isinstance(entries, str):
        return None
    if now.timestamp() >= expires:
        return None
    return TicketClaims(user_id, carrier_type, carrier_id, scope, entries, fingerprint, expires)
