"""Keyset pagination with opaque cursors (EF-INT-001).

A cursor wraps the sort key of the last row served; the next page is "rows after this key",
so a row inserted or removed mid-walk neither repeats nor skips another. Offsets are never
used. A cursor is opaque to clients and carries nothing secret: it is not signed, but it is
validated strictly and every query that uses it binds its values as parameters.
"""

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fastapi import Query
from pydantic import BaseModel

from shelfhold.interfaces.http_api.problems import ProblemError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_MAX_CURSOR_CHARS = 512
_MAX_KEY_PARTS = 8

CursorValue = str | int


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None  # absent on the last page


@dataclass(frozen=True)
class PageParams:
    limit: int
    after: tuple[CursorValue, ...] | None  # decoded sort key of the last row already served


def encode_cursor(key: Sequence[CursorValue]) -> str:
    raw = json.dumps(list(key), separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(token: str) -> tuple[CursorValue, ...]:
    try:
        if not token or len(token) > _MAX_CURSOR_CHARS or not token.isascii():
            raise ValueError("size")
        padded = token + "=" * (-len(token) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        ok = isinstance(value, list) and 0 < len(value) <= _MAX_KEY_PARTS
        if not ok or not all(type(p) in (str, int) for p in value):
            raise ValueError("shape")
    except (ValueError, binascii.Error) as exc:
        raise ProblemError("invalid_cursor") from exc
    return tuple(value)


def page_params(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> PageParams:
    return PageParams(limit=limit, after=decode_cursor(cursor) if cursor else None)


def make_page[T](
    rows: Sequence[T], limit: int, key_of: Callable[[T], Sequence[CursorValue]]
) -> Page[T]:
    """`rows` were fetched with `limit + 1`: the extra row only says there is a next page."""
    items = list(rows[:limit])
    more = len(rows) > limit
    return Page[T](items=items, next_cursor=encode_cursor(key_of(items[-1])) if more else None)
