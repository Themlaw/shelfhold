"""`GET /api/v1/thumbnails/{content_hash}/{size}`: a cover at 160, 320 or 640 pixels (EF-BIB-020).

The address is the hash of the cover (64 hex digits, what `cover_hash` reads in an Edition's
`cover` field) and the size. The answer is WebP, `Cache-Control: private, max-age=86400,
immutable`, an ETag and `Vary: Cookie`: the browser keeps it a day, a shared cache keeps nothing.

Order of the checks, which is the whole point (ENF-SEC-010, EF-USR-006):

1. a session (a browser shows a cover with its cookie; a key is refused, hers or an adult's);
2. the reader's own scope, read from the database now, and the one filter: an Edition she may see
   must point at this cover;
3. only then the tag is compared (304) and only then is anything read or made.

A cover that is hidden, absent, unreferenced, malformed, of a size that does not exist or whose
file is gone is the one 404 of an absent thing: no tag, no length, no date, no work done for it.
So a child that presents the ETag of an adult cover learns nothing, and neither does a guess. The
route only reads: there is no verb to make, remake or drop a thumbnail.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import Engine

from shelfhold.catalog.public import cover_ref_visible
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.interfaces.http_api.auth import require_session
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.library.public import CACHE_CONTROL, THUMBNAIL_SIZES, ThumbnailService, etag_of

__all__ = ["build_thumbnails_router"]

_HEADERS = {"Cache-Control": CACHE_CONTROL, "Vary": "Cookie", "X-Content-Type-Options": "nosniff"}
_ABSENT = {"Cache-Control": "no-store"}
_HEX = frozenset("0123456789abcdef")


def _absent() -> ProblemError:
    return ProblemError("not_found", headers=_ABSENT)


def _size_of(raw: str) -> int | None:
    value = int(raw) if raw.isascii() and raw.isdigit() else None
    return value if value in THUMBNAIL_SIZES else None


def _matches(header: str | None, etag: str) -> bool:
    """`If-None-Match`: `*`, or a list of tags, weak or not (a 304 needs the weak comparison)."""
    if header is None:
        return False
    if header.strip() == "*":
        return True
    return any(tag.strip().removeprefix("W/") == etag for tag in header.split(","))


def build_thumbnails_router(
    get_engine: Callable[[], Engine], get_service: Callable[[], ThumbnailService]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["library"])

    @router.get(
        "/thumbnails/{content_hash}/{size}",
        response_class=Response,
        responses={
            200: {"content": {"image/webp": {}}, "description": "The thumbnail, WebP."},
            304: {"description": "The reader holds this thumbnail (the tag matches)."},
            404: {"description": "Not found: hidden, unknown, or no such size."},
        },
    )
    def thumbnail(
        content_hash: str,
        size: str,
        request: Request,
        principal: Annotated[Principal, Depends(require_session)],
    ) -> Response:
        width = _size_of(size)
        if width is None or len(content_hash) != 64 or not set(content_hash) <= _HEX:
            raise _absent()
        with get_engine().connect() as connection:
            scope = reader_scope(connection, principal.user_id)
            visible = cover_ref_visible(connection, scope, f"covers/{content_hash}.webp")
        if not visible:  # before the tag, before the disk, before the worker
            raise _absent()
        tag = etag_of(content_hash, width)
        if _matches(request.headers.get("if-none-match"), tag):
            return Response(status_code=304, headers={**_HEADERS, "ETag": tag})
        made = get_service().get(content_hash, width)
        if made is None:
            raise _absent()
        return Response(made.data, media_type="image/webp", headers={**_HEADERS, "ETag": made.etag})

    return router
