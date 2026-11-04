"""`ETag` and `?since=` on lists (EF-INT-001, volet coupable of M1a).

`?since=` takes an opaque token backed by the sequence of the `event` table, never a timestamp.
The M1a token carries the sequence only; the removals of a list (deleted, purged, merged,
visibility lost) arrive with M3, before the 1.0 freeze. A token older than the event retention
answers `resync_required`, symmetric to the SSE `resync_required`.
"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from fastapi import Query
from starlette.requests import Request
from starlette.responses import Response

from shelfhold.interfaces.http_api.problems import ProblemError

_PREFIX = "s1."
_MAX_TOKEN_CHARS = 64


@dataclass(frozen=True)
class Since:
    seq: int

    def require_not_older_than(self, retention_floor_seq: int) -> None:
        """The events after the token must still exist: otherwise the client reloads the list."""
        if self.seq < retention_floor_seq:
            raise ProblemError("resync_required")


def encode_since(seq: int) -> str:
    raw = str(seq).encode("ascii")
    return _PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_since(token: str) -> Since:
    try:
        if len(token) > _MAX_TOKEN_CHARS or not token.startswith(_PREFIX):
            raise ValueError("shape")
        body = token[len(_PREFIX) :]
        text = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
        seq = int(text.decode("ascii"))
        if seq < 0 or str(seq) != text.decode("ascii"):
            raise ValueError("range")
    except (ValueError, binascii.Error) as exc:
        raise ProblemError("invalid_cursor") from exc
    return Since(seq=seq)


def since_param(since: str | None = Query(None)) -> Since | None:
    return decode_since(since) if since is not None else None


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
