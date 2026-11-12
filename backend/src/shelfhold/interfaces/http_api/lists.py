"""`ETag` and `?since=` on lists (EF-INT-001; the M3 volet, L11).

`?since=` takes an opaque, signed token backed by the sequence of the `event` table, never a
timestamp. What a list reports with it is its removals (a deleted object, an object that left the
reader's view), limited to what that reader could see when the token was issued: that rule lives
in `catalog.application.changes`, which reads the events; this module only holds the token.

The token is `s2.<sequence>.<signature>`: the signature (HMAC-SHA256 under a key of the instance,
cut to 16 bytes) covers the list the token is for and the sequence, so a token cannot be edited,
carried to another list or minted by a client. A token older than the event retention answers
`resync_required` (410), symmetric to the SSE `resync_required`; one that is not ours,
`invalid_cursor`.
"""

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from fastapi import Query
from starlette.requests import Request
from starlette.responses import Response

from shelfhold.interfaces.http_api.problems import ProblemError

_PREFIX = "s2."
MAX_TOKEN_CHARS = 96
_MAC_BYTES = 16
_MAX_SEQ = 2**63 - 1


@dataclass(frozen=True)
class Since:
    seq: int

    def require_not_older_than(self, retention_floor_seq: int) -> None:
        """The events after the token must still exist: otherwise the client reloads the list."""
        if self.seq < retention_floor_seq:
            raise ProblemError("resync_required")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mac(key: bytes, kind: str, seq: int) -> str:
    body = f"since:{kind}:{seq}".encode("ascii")
    return _b64(hmac.new(key, body, hashlib.sha256).digest()[:_MAC_BYTES])


def encode_since(seq: int, *, key: bytes, kind: str) -> str:
    if not 0 <= seq <= _MAX_SEQ:
        raise ValueError("a sequence is a non-negative 63-bit integer")
    return f"{_PREFIX}{_b64(str(seq).encode('ascii'))}.{_mac(key, kind, seq)}"


def decode_since(token: str, *, key: bytes, kind: str) -> Since:
    try:
        if len(token) > MAX_TOKEN_CHARS or not token.isascii() or not token.startswith(_PREFIX):
            raise ValueError("shape")
        body, _, mac = token[len(_PREFIX) :].partition(".")
        text = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
        seq = int(text.decode("ascii"))
        if not 0 <= seq <= _MAX_SEQ or str(seq) != text.decode("ascii"):
            raise ValueError("range")
        expected = _mac(key, kind, seq)
        if not hmac.compare_digest(mac.encode("ascii"), expected.encode("ascii")):
            raise ValueError("signature")
        if token != encode_since(seq, key=key, kind=kind):  # one spelling per token
            raise ValueError("spelling")
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise ProblemError("invalid_cursor") from exc
    return Since(seq=seq)


def since_param(since: str | None = Query(None)) -> str | None:
    """The raw token: the route decodes it, since it knows its list and its key."""
    return since


def content_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    # If-None-Match uses the weak comparison (RFC 9110 §13.1.2): `W/` is ignored.
    candidates = [part.strip().removeprefix("W/") for part in header.split(",")]
    return "*" in candidates or etag in candidates


def conditional_json(request: Request, payload: Any) -> Response:
    """JSON answer with a content ETag; `304` when the client already holds this exact list."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()
    etag = content_etag(body)
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="application/json", headers={"ETag": etag})
