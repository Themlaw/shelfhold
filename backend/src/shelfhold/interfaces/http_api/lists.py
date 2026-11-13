"""`ETag` and `?since=` on lists (EF-INT-001; the M3 volet, L11).

`?since=` takes an opaque token backed by the sequence of the `event` table, never a timestamp. What
a list reports with it is its removals (a deleted object, an object that left the reader's view),
limited to what that reader could see when the token was issued: that rule lives in
`catalog.application.changes`, which reads the events; this module only holds the token.

The token is `s3.<nonce and ciphertext>`: XChaCha20-Poly1305 under a key of the instance (derived
from the keyring), a fresh random nonce each time, a fixed width. The client cannot read it,
edit it, compare two of them or make one: two tokens of the same moment are unrelated strings,
and the position inside says nothing a reader could learn about other profiles' activity. It
holds:

- the position in the event sequence;
- the `restore_epoch` of the instance and a marker of the history at that position (a restored
  backup is another history: its tokens answer `resync_required`);
- the scope of the reader (adult, or her ceiling): a ceiling or a role that changed since is a
  change of what she sees that no event of a Work tells, so the route asks for a resync.

The list the token is for is part of the associated data: a token of one list opens on no other.
A token that does not open is `invalid_cursor`; one older than the event retention,
`resync_required` (410), symmetric to the SSE `resync_required`.
"""

import base64
import binascii
import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any, Final

import nacl.bindings
import nacl.exceptions
import nacl.utils
from fastapi import Query
from starlette.requests import Request
from starlette.responses import Response

from shelfhold.interfaces.http_api.problems import ProblemError

_PREFIX = "s3."
MAX_TOKEN_CHARS = 96
_NONCE_BYTES = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES
_TAG_BYTES = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_ABYTES
_BODY = struct.Struct(">QI6sH")  # position, restore epoch, history marker, scope
_MAX_SEQ = 2**63 - 1
_MAX_EPOCH = 2**32 - 1
_MAX_SCOPE = 2**16 - 1
_DOMAIN: Final = b"shelfhold/since-token/v1/"
NO_MARKER: Final = b"\x00" * 6


@dataclass(frozen=True)
class Since:
    seq: int
    epoch: int = 0
    marker: bytes = NO_MARKER
    scope: int = 0

    def require_not_older_than(self, retention_floor_seq: int) -> None:
        """The events after the token must still exist: otherwise the client reloads the list."""
        if self.seq < retention_floor_seq:
            raise ProblemError("resync_required")


def _aad(kind: str) -> bytes:
    return _DOMAIN + kind.encode("ascii")


def encode_since(
    seq: int, *, key: bytes, kind: str, epoch: int = 0, marker: bytes = NO_MARKER, scope: int = 0
) -> str:
    if not 0 <= seq <= _MAX_SEQ or not 0 <= epoch <= _MAX_EPOCH or not 0 <= scope <= _MAX_SCOPE:
        raise ValueError("sequence_out_of_range")
    if len(marker) != 6:
        raise ValueError("marker_length")
    nonce = nacl.utils.random(_NONCE_BYTES)
    sealed = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
        _BODY.pack(seq, epoch, marker, scope), _aad(kind), nonce, key
    )
    return _PREFIX + base64.urlsafe_b64encode(nonce + sealed).rstrip(b"=").decode("ascii")


def decode_since(token: str, *, key: bytes, kind: str) -> Since:
    try:
        if len(token) > MAX_TOKEN_CHARS or not token.isascii() or not token.startswith(_PREFIX):
            raise ValueError("shape")
        body = token[len(_PREFIX) :]
        raw = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
        if len(raw) != _NONCE_BYTES + _BODY.size + _TAG_BYTES:
            raise ValueError("width")
        if _PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
            raise ValueError("spelling")  # one spelling per token
        opened = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            raw[_NONCE_BYTES:], _aad(kind), raw[:_NONCE_BYTES], key
        )
        seq, epoch, marker, scope = _BODY.unpack(opened)
        if seq > _MAX_SEQ:
            raise ValueError("range")
    except (ValueError, binascii.Error, UnicodeError, nacl.exceptions.CryptoError) as exc:
        raise ProblemError("invalid_cursor") from exc
    return Since(seq=seq, epoch=epoch, marker=marker, scope=scope)


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
