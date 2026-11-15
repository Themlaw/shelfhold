"""`/opds`: the OPDS 1.2 feed of Shelfhold, behind a device token (M4 L48, ADR-0053 §1, §4, §8).

Every route takes `require_device_basic`: HTTP Basic, over TLS, for the profile of the token. The
profile is read from the database at each request (`reader_scope`), and the one filter of the
catalogue (M3 L09) is a condition of every query: a feed, a page, a count, a search hit, a cover
and a download link are made of what the profile may see, and nothing is filtered afterwards.

What is hidden and what does not exist are one answer (404): a Work, an Edition, a shelf, a series
and a cover alike. A total is the number of visible rows; a `next` link exists only when a visible
row follows; a search echoes nothing of what it did not find. Every link is a path under
`{prefix}/opds` (`links.Paths`), and none carries the secret, a host or a title.

The block of a series (L49a) is plugged in through `bundle.SeriesBundleProvider`: this module
offers a download only for an Edition the reader may see, asks the provider only then, and cleans
the name it gets.
"""

import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import (
    ReaderScope,
    VisibleWork,
    count_visible_works,
    list_visible_series,
    list_visible_works,
    search_page,
    shelf_open_to,
    visible_edition_ids,
    visible_editions_of_work,
    visible_series_title,
    visible_work_title,
)
from shelfhold.export.application.shelves import list_shelves
from shelfhold.i18n import t_lang
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.infrastructure.http_headers import CSP_SANDBOX, attachment_disposition
from shelfhold.interfaces.http_api.device_auth import require_device_basic
from shelfhold.interfaces.http_api.opds.bundle import SeriesBundleProvider
from shelfhold.interfaces.http_api.opds.feed import (
    FEED_TYPE,
    Entry,
    Kind,
    Link,
    build_feed,
    feed_type,
    opensearch_description,
)
from shelfhold.interfaces.http_api.opds.links import Paths, mount_prefix, safe_filename
from shelfhold.interfaces.http_api.problems import ProblemError, language_from_headers
from shelfhold.interfaces.http_api.thumbnails import thumbnail_response
from shelfhold.library.public import ThumbnailService, cover_hash

__all__ = ["build_opds_router"]

PAGE = 50
MAX_OFFSET = 100_000
MAX_QUERY = 200
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_ACQUISITION = "http://opds-spec.org/acquisition"
_THUMBNAIL = "http://opds-spec.org/image/thumbnail"
_IMAGE = "http://opds-spec.org/image"
_SUBSECTION = "subsection"
_HEADERS = {
    "Cache-Control": "private, no-cache",
    "Vary": "Authorization",
    "X-Content-Type-Options": "nosniff",
}

Device = Annotated[Principal, Depends(require_device_basic)]
Limit = Annotated[int, Query(ge=1, le=PAGE)]
Offset = Annotated[int, Query(ge=0, le=MAX_OFFSET)]


def _absent() -> ProblemError:
    """Hidden, unknown and malformed are the same answer."""
    return ProblemError("not_found", headers={"Cache-Control": "no-store"})


def _ulid(raw: str) -> str:
    if not _ULID.fullmatch(raw):
        raise _absent()
    return raw


class _Page:
    """The links of one page of a list: `self`, `next` only when a visible row follows."""

    def __init__(self, offset: int, limit: int, make: Callable[..., str]) -> None:
        self.offset, self.limit, self.make = offset, limit, make

    def _href(self, offset: int) -> str:
        return self.make(offset=offset, limit=None if self.limit == PAGE else self.limit)

    def links(self, *, has_next: bool, kind: Kind = "acquisition") -> list[Link]:
        kind_type = feed_type(kind)
        found = [Link("self", self._href(self.offset), kind_type)]
        if self.offset > 0:
            found.append(Link("previous", self._href(max(0, self.offset - self.limit)), kind_type))
        if has_next:
            found.append(Link("next", self._href(self.offset + self.limit), kind_type))
        return found


def build_opds_router(
    get_engine: Callable[[], Engine],
    get_thumbnails: Callable[[], ThumbnailService],
    get_bundles: Callable[[], SeriesBundleProvider | None],
    now: Callable[[], datetime],
) -> APIRouter:
    router = APIRouter(prefix="/opds", tags=["opds"])

    def _stamp() -> str:
        return now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _say(request: Request, principal: Principal, key: str, **params: object) -> str:
        return t_lang(
            f"opds.{key}", language=language_from_headers(request.headers, principal), **params
        )

    def _scope(connection: Connection, principal: Principal) -> ReaderScope:
        return reader_scope(connection, principal.user_id)

    def _paths(request: Request) -> Paths:
        return Paths(mount_prefix(request.scope))

    def _atom(body: str, kind: Kind) -> Response:
        return Response(body, media_type=feed_type(kind), headers=_HEADERS)

    def _start(paths: Paths) -> list[Link]:
        return [
            Link("start", paths.root(), feed_type("navigation")),
            Link("search", paths.opensearch(), "application/opensearchdescription+xml"),
        ]

    def _work_entries(works: list[VisibleWork], paths: Paths) -> list[Entry]:
        found = []
        for work in works:
            entry_links = [Link(_SUBSECTION, paths.work(work.work_id), feed_type("acquisition"))]
            digest = cover_hash(work.cover_ref) if work.cover_ref else None
            if digest:
                entry_links += [
                    Link(_THUMBNAIL, paths.cover(digest, 160), "image/webp"),
                    Link(_IMAGE, paths.cover(digest, 640), "image/webp"),
                ]
            found.append(
                Entry(f"urn:shelfhold:work:{work.work_id}", work.title, _stamp(), entry_links)
            )
        return found

    def _works_feed(
        request: Request, principal: Principal, *, feed_id: str, title: str,
        make: Callable[..., str], offset: int, limit: int, recent: bool = False,
        shelf_id: str | None = None, series_id: str | None = None,
    ) -> Response:  # fmt: skip
        paths = _paths(request)
        with get_engine().connect() as connection:
            scope = _scope(connection, principal)
            page = list_visible_works(
                connection, scope, limit=limit + 1, offset=offset, recent=recent,
                shelf_id=shelf_id, series_id=series_id,
            )  # fmt: skip
            total = count_visible_works(connection, scope, shelf_id=shelf_id, series_id=series_id)
        shown = page[:limit]
        links = [*_Page(offset, limit, make).links(has_next=len(page) > limit), *_start(paths)]
        links.append(Link("up", paths.root(), feed_type("navigation")))
        body = build_feed(
            feed_id=feed_id, title=title, updated=_stamp(), kind="acquisition", links=links,
            entries=_work_entries(shown, paths), total=total, start=offset, per_page=limit,
        )  # fmt: skip
        return _atom(body, "acquisition")

    @router.get(
        "", operation_id="opds_root", response_class=Response,
        responses={200: {"content": {FEED_TYPE: {}}, "description": "navigation feed"}},
    )  # fmt: skip
    def root(request: Request, principal: Device) -> Response:
        paths = _paths(request)
        nav = feed_type("navigation")
        door = [
            ("shelves", paths.shelves(), nav), ("series", paths.series_list(), nav),
            ("works", paths.works(), feed_type("acquisition")),
            ("recent", paths.recent(), feed_type("acquisition")),
        ]  # fmt: skip
        entries = [
            Entry(f"urn:shelfhold:opds:{key}", _say(request, principal, key), _stamp(),
                  [Link(_SUBSECTION, href, kind)])
            for key, href, kind in door
        ]  # fmt: skip
        body = build_feed(
            feed_id="urn:shelfhold:opds:root", title=_say(request, principal, "root_title"),
            updated=_stamp(), kind="navigation",
            links=[Link("self", paths.root(), nav), *_start(paths)], entries=entries,
        )  # fmt: skip
        return _atom(body, "navigation")

    @router.get(
        "/opensearch.xml", operation_id="opds_opensearch", response_class=Response,
        responses={200: {"content": {"application/opensearchdescription+xml": {}}}},
    )  # fmt: skip
    def opensearch(request: Request, principal: Device) -> Response:
        body = opensearch_description(
            template=_paths(request).search_template(),
            title=_say(request, principal, "search_description"),
        )
        return Response(body, media_type="application/opensearchdescription+xml", headers=_HEADERS)

    def _listed_shelves(connection: Connection, scope: ReaderScope) -> list[tuple[str, str]]:
        found = []
        for shelf in list_shelves(connection):
            if shelf_open_to(scope, shelf.max_age_rating) and count_visible_works(
                connection, scope, shelf_id=shelf.id
            ):
                found.append((shelf.id, shelf.name))
        return found

    @router.get("/shelves", operation_id="opds_shelves", response_class=Response)
    def shelves(request: Request, principal: Device) -> Response:
        paths = _paths(request)
        with get_engine().connect() as connection:
            listed = _listed_shelves(connection, _scope(connection, principal))
        entries = [
            Entry(f"urn:shelfhold:shelf:{shelf_id}", name, _stamp(),
                  [Link(_SUBSECTION, paths.shelf(shelf_id), feed_type("acquisition"))])
            for shelf_id, name in listed
        ]  # fmt: skip
        body = build_feed(
            feed_id="urn:shelfhold:opds:shelves", title=_say(request, principal, "shelves"),
            updated=_stamp(), kind="navigation",
            links=[Link("self", paths.shelves(), feed_type("navigation")), *_start(paths)],
            entries=entries,
        )  # fmt: skip
        return _atom(body, "navigation")

    @router.get("/shelves/{shelf_id}", operation_id="opds_shelf", response_class=Response)
    def shelf(
        shelf_id: str, request: Request, principal: Device, limit: Limit = PAGE, offset: Offset = 0
    ) -> Response:
        _ulid(shelf_id)
        with get_engine().connect() as connection:
            listed = _listed_shelves(connection, _scope(connection, principal))
        name = dict(listed).get(shelf_id)
        if name is None:  # a shelf the reader is not shown is a shelf that does not exist
            raise _absent()
        return _works_feed(
            request, principal, feed_id=f"urn:shelfhold:shelf:{shelf_id}", title=name,
            make=lambda **q: _paths(request).shelf(shelf_id, **q),
            offset=offset, limit=limit, shelf_id=shelf_id,
        )  # fmt: skip

    @router.get("/series", operation_id="opds_series_list", response_class=Response)
    def series_list(
        request: Request, principal: Device, limit: Limit = PAGE, offset: Offset = 0
    ) -> Response:
        paths = _paths(request)
        with get_engine().connect() as connection:
            every = list_visible_series(connection, _scope(connection, principal))
        shown = every[offset : offset + limit]
        entries = [
            Entry(f"urn:shelfhold:series:{series_id}", title, _stamp(),
                  [Link(_SUBSECTION, paths.series(series_id), feed_type("acquisition"))])
            for series_id, title in shown
        ]  # fmt: skip
        links = [
            *_Page(offset, limit, paths.series_list).links(
                has_next=offset + limit < len(every), kind="navigation"
            ),
            *_start(paths),
        ]
        body = build_feed(
            feed_id="urn:shelfhold:opds:series", title=_say(request, principal, "series"),
            updated=_stamp(), kind="navigation", links=links, entries=entries,
            total=len(every), start=offset, per_page=limit,
        )  # fmt: skip
        return _atom(body, "navigation")

    @router.get("/series/{series_id}", operation_id="opds_series", response_class=Response)
    def series(
        series_id: str, request: Request, principal: Device,
        limit: Limit = PAGE, offset: Offset = 0,
    ) -> Response:  # fmt: skip
        _ulid(series_id)
        with get_engine().connect() as connection:
            title = visible_series_title(connection, _scope(connection, principal), series_id)
        if title is None:
            raise _absent()
        return _works_feed(
            request, principal, feed_id=f"urn:shelfhold:series:{series_id}", title=title,
            make=lambda **q: _paths(request).series(series_id, **q),
            offset=offset, limit=limit, series_id=series_id,
        )  # fmt: skip

    @router.get("/works", operation_id="opds_works", response_class=Response)
    def works(
        request: Request, principal: Device, limit: Limit = PAGE, offset: Offset = 0
    ) -> Response:
        return _works_feed(
            request, principal, feed_id="urn:shelfhold:opds:works",
            title=_say(request, principal, "works"),
            make=lambda **q: _paths(request).works(**q), offset=offset, limit=limit,
        )  # fmt: skip

    @router.get("/recent", operation_id="opds_recent", response_class=Response)
    def recent(
        request: Request, principal: Device, limit: Limit = PAGE, offset: Offset = 0
    ) -> Response:
        return _works_feed(
            request, principal, feed_id="urn:shelfhold:opds:recent",
            title=_say(request, principal, "recent"),
            make=lambda **q: _paths(request).recent(**q), offset=offset, limit=limit, recent=True,
        )  # fmt: skip

    @router.get("/works/{work_id}", operation_id="opds_work", response_class=Response)
    def work(work_id: str, request: Request, principal: Device) -> Response:
        _ulid(work_id)
        paths = _paths(request)
        provider = get_bundles()
        with get_engine().connect() as connection:
            scope = _scope(connection, principal)
            title = visible_work_title(connection, scope, work_id)
            editions = visible_editions_of_work(connection, scope, work_id) if title else []
        if title is None:
            raise _absent()
        entries = []
        for (
            edition
        ) in editions:  # only Editions the reader may see: the provider is asked for no other
            offer = (
                provider.offer(
                    user_id=principal.user_id, scope=scope, edition_id=edition.edition_id
                )
                if provider is not None
                else None
            )
            if offer is not None:
                label = safe_filename(offer.filename).rsplit(".", 1)[0]
            elif len(editions) > 1:
                label = _say(request, principal, "complete_in", title=title, lang=edition.lang)
            else:
                label = _say(request, principal, "complete", title=title)
            entry_links: list[Link] = []
            digest = cover_hash(edition.cover_ref) if edition.cover_ref else None
            if digest:
                entry_links += [
                    Link(_THUMBNAIL, paths.cover(digest, 160), "image/webp"),
                    Link(_IMAGE, paths.cover(digest, 640), "image/webp"),
                ]
            if offer is not None:
                entry_links.append(
                    Link(_ACQUISITION, paths.complete(edition.edition_id), offer.media_type,
                         length=offer.size)
                )  # fmt: skip
            entries.append(
                Entry(f"urn:shelfhold:edition:{edition.edition_id}", label, _stamp(), entry_links,
                      summary=offer.notice if offer is not None else None)
            )  # fmt: skip
        body = build_feed(
            feed_id=f"urn:shelfhold:work:{work_id}", title=title, updated=_stamp(),
            kind="acquisition",
            links=[Link("self", paths.work(work_id), feed_type("acquisition")), *_start(paths)],
            entries=entries,
        )  # fmt: skip
        return _atom(body, "acquisition")

    @router.get("/search", operation_id="opds_search", response_class=Response)
    def search(
        request: Request, principal: Device,
        q: Annotated[str, Query(max_length=MAX_QUERY)] = "",
        limit: Limit = PAGE, offset: Offset = 0,
    ) -> Response:  # fmt: skip
        paths = _paths(request)
        words = q.strip()
        with get_engine().connect() as connection:
            found = search_page(
                connection, words, limit=limit, offset=offset, scope=_scope(connection, principal)
            )
        works = [VisibleWork(h.work_id, h.title, h.kind) for h in found.hits]
        links = [
            *_Page(offset, limit, lambda **p: paths.search(words, **p)).links(
                has_next=offset + limit < found.total
            ),
            *_start(paths),
        ]
        body = build_feed(
            feed_id="urn:shelfhold:opds:search", title=_say(request, principal, "results"),
            updated=_stamp(), kind="acquisition", links=links,
            entries=_work_entries(works, paths), total=found.total, start=offset, per_page=limit,
        )  # fmt: skip
        return _atom(body, "acquisition")

    @router.get(
        "/covers/{content_hash}/{size}", operation_id="opds_cover", response_class=Response,
        responses={200: {"content": {"image/webp": {}}}, 304: {"description": "not-modified"}},
    )  # fmt: skip
    def cover(content_hash: str, size: str, request: Request, principal: Device) -> Response:
        return thumbnail_response(
            engine=get_engine(), service=get_thumbnails(), user_id=principal.user_id,
            content_hash=content_hash, size=size,
            if_none_match=request.headers.get("if-none-match"), vary="Authorization",
        )  # fmt: skip

    @router.get(
        "/editions/{edition_id}/complete", operation_id="opds_complete", response_class=Response,
        responses={200: {"content": {"application/vnd.comicbook+zip": {}}}},
    )  # fmt: skip
    def complete(edition_id: str, principal: Device) -> Response:
        _ulid(edition_id)
        provider = get_bundles()
        if provider is None:  # nothing to serve is the answer of an absent thing
            raise _absent()
        with get_engine().connect() as connection:
            scope = _scope(connection, principal)
            visible = visible_edition_ids(connection, scope, [edition_id])
        if not visible:  # read now, not when the feed was: the URL is no capability
            raise _absent()
        download = provider.open(user_id=principal.user_id, scope=scope, edition_id=edition_id)
        if download is None:
            raise _absent()
        headers = {
            **_HEADERS,
            "Content-Length": str(download.size),
            "Content-Disposition": attachment_disposition(safe_filename(download.filename)),
            "Content-Security-Policy": CSP_SANDBOX,
        }
        return StreamingResponse(download.chunks, media_type=download.media_type, headers=headers)

    return router
