"""OPDS-PSE: the pages of an exported volume, one request each (M5 L18, EF-INT-007, EF-BIB-015).

`GET /opds/pse/{item}/{page}`, behind the device token like the rest of `/opds`; `page` counts from
0, as the PSE specification does. The feed offers it as a `pse:stream` link (`routes.work`).

What a request does, in this order, and nothing before the decision:
1. the token says who asks (`require_device_basic`: a web session, a key of the API, no token and a
   revoked one are refused there);
2. the identifiers are well formed, else the answer of an absent thing;
3. `PageStream.page` judges the profile again, now, on the item and on the content it resolves to,
   and reads the page from the kept copy: a child gets nothing of a hidden Work, not even a
   different 404 (same status, same code, same length as an item that does not exist);
4. only for a page the profile may see, the format: the client's `Accept` decides (the native
   page when it takes it, else a JPEG or PNG rendition of L20a, else 406). A refusal for format
   comes after the visibility, so it is never an oracle on what exists.

The bytes go out by `content_response` (sandboxed, `nosniff`, never an attachment for a raster
image). Nothing is written, ever: no position and no mark (ENF-SEC-013): PSE is a read-only door.
"""

import math
import re
from collections.abc import Callable
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.content import content_response
from shelfhold.interfaces.http_api.device_auth import require_device_basic
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.library.public import (
    DECODABLE_SOURCES,
    KIND_MEDIA,
    ImageRejected,
    RemoteProcessorRefusedError,
    RenditionParams,
    RenditionService,
    TombstonedContentError,
    UnknownSourceError,
    UnsupportedSourceError,
    source_format,
)
from shelfhold.reading.application.page_stream import PageStream, StreamPage
from shelfhold.reading.application.progress import UnknownCarrierError

__all__ = ["STREAM_REL", "Choice", "add_pse_routes", "negotiate"]

STREAM_REL: Final = "http://vaemendis.net/opds-pse/stream"
STREAM_TYPE: Final = "image/jpeg"  # the type a stream link announces: what every client takes
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_PAGE = re.compile(r"[0-9]{1,6}")  # ASCII digits: `str.isdigit` also takes other scripts
_MEDIA = re.compile(r"[a-z*][a-z0-9!#$&^_.+*-]{0,126}/[a-z0-9*][a-z0-9!#$&^_.+*-]{0,126}")
_MAX_ACCEPT = 2048
_KINDS: Final = ("jpeg", "png")  # the order of preference when the client has none of its own
_VARY = "Authorization, Accept"
_NO_STORE = {"Cache-Control": "no-store", "Vary": _VARY}

Choice = Literal["native", "jpeg", "png"]
Device = Annotated[Principal, Depends(require_device_basic)]


def _ranges(header: str) -> list[tuple[str, float]]:
    """The media ranges of an `Accept` header with their weight; a part that is not one is left
    out, as is a weight that is not a number (it is 0: not acceptable)."""
    found: list[tuple[str, float]] = []
    for part in header[:_MAX_ACCEPT].split(","):
        pieces = [p.strip() for p in part.split(";")]
        media = pieces[0].lower()
        if not _MEDIA.fullmatch(media):
            continue
        weight = 1.0
        for parameter in pieces[1:]:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
                weight = min(max(weight, 0.0), 1.0) if math.isfinite(weight) else 0.0
        found.append((media, weight))
    return found


def _weight(ranges: list[tuple[str, float]], media: str) -> float:
    """The weight the client gives `media`: the most specific range that names it decides
    (`image/webp` over `image/*` over `*/*`), 0 when none does."""
    best, weight = 0, 0.0
    for known, value in ranges:
        rank = (
            3
            if known == media
            else 2
            if known == media.split("/", 1)[0] + "/*"
            else 1
            if known == "*/*"
            else 0
        )
        if rank > best or (rank == best and rank > 0 and value > weight):
            best, weight = rank, value
    return weight


def negotiate(accept: str | None, native: str) -> Choice | None:
    """The format of a page of media type `native` for a client that sent `accept`: `native` when
    it takes it, a rendition kind when it does not and the page can be converted to one it takes,
    else None. No `Accept`, or one with no usable range, takes everything (RFC 9110)."""
    ranges = _ranges(accept) if accept else []
    if not ranges or _weight(ranges, native.lower()) > 0:
        return "native"
    if source_format("", native) not in DECODABLE_SOURCES:
        return None  # a JPEG, a PNG or a GIF is not converted: only what a client may lack is
    wanted = [(_weight(ranges, KIND_MEDIA[kind]), -i, kind) for i, kind in enumerate(_KINDS)]
    weight, _order, kind = max(wanted)
    return None if weight <= 0 else ("jpeg" if kind == "jpeg" else "png")


def pse_template(base: str, item_id: str) -> str:
    """The href of a `pse:stream` link, a template a client fills with the page number."""
    return f"{base}/pse/{item_id}/{{pageNumber}}"


def add_pse_routes(
    router: APIRouter,
    *,
    get_streams: Callable[[], PageStream],
    get_renditions: Callable[[], RenditionService],
) -> None:
    def absent() -> ProblemError:
        return ProblemError("not_found", headers=_NO_STORE)

    @router.get(
        "/pse/{item_id}/{page}",
        operation_id="opds_pse_page",
        response_class=Response,
        responses={
            200: {
                "content": {m: {} for m in ("image/jpeg", "image/png", "image/webp", "image/avif")},
                "description": "page",
            },
            404: {"description": "not-found"},
            406: {"description": "not-acceptable"},
        },
    )
    def pse_page(item_id: str, page: str, request: Request, principal: Device) -> Response:
        if not _ULID.fullmatch(item_id) or not _PAGE.fullmatch(page):
            raise absent()
        try:
            served = get_streams().page(principal.user_id, item_id, int(page))
        except UnknownCarrierError:
            raise absent() from None
        choice = negotiate(request.headers.get("accept"), served.media_type)
        if choice is None:
            raise ProblemError("not_acceptable", headers=_NO_STORE)
        if choice == "native":
            made = content_response(served.body, media_type=served.media_type, filename=served.name)
        else:
            made = _converted(get_renditions(), served, choice, absent)
        made.headers["Vary"] = _VARY
        return made


def _converted(
    service: RenditionService, served: StreamPage, kind: str, absent: Callable[[], ProblemError]
) -> Response:
    """The page as `kind`: made now or found in the cache, read back checked. What the page cannot
    become is 406 (a header the worker refuses, a file that does not decode); a source whose purge
    began, or that is gone, is an absent thing."""
    try:
        rendition = service.render(
            served.content_id, served.body, RenditionParams(kind=kind, entry=served.name[:512])
        )
        data = service.read(rendition)
    except (UnknownSourceError, TombstonedContentError):
        raise absent() from None
    except (ImageRejected, UnsupportedSourceError, RemoteProcessorRefusedError):
        raise ProblemError("not_acceptable", headers=_NO_STORE) from None
    if data is None:
        raise ProblemError("not_acceptable", headers=_NO_STORE)
    return content_response(data, media_type=rendition.media_type, filename=f"page.{kind}")
