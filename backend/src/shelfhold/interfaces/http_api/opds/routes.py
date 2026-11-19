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
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import Connection, Engine
from starlette.concurrency import iterate_in_threadpool

from shelfhold.catalog.public import (
    ReaderScope,
    SelectionNotFoundError,
    Selections,
    SelectionTooSlowError,
    SelectionViewer,
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
from shelfhold.export.application.shelves import list_shelves_open_to
from shelfhold.i18n import t_lang
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.infrastructure.http_headers import CSP_SANDBOX, attachment_disposition
from shelfhold.interfaces.http_api.device_auth import require_device_basic
from shelfhold.interfaces.http_api.opds.bundle import (
    BundleBusyError,
    BundleDownload,
    BundleGoneError,
    BundleNotFoundError,
    SeriesBundleProvider,
)
from shelfhold.interfaces.http_api.opds.feed import (
    FEED_TYPE,
    Entry,
    FeedDoc,
    FeedFormat,
    Kind,
    Link,
    build_feed,
    feed_type,
    opensearch_description,
)
from shelfhold.interfaces.http_api.opds.links import Paths, mount_prefix, safe_filename
from shelfhold.interfaces.http_api.opds.pse import STREAM_REL, STREAM_TYPE, add_pse_routes
from shelfhold.interfaces.http_api.opds.v2 import JsonFeed
from shelfhold.interfaces.http_api.problems import ProblemError, language_from_headers
from shelfhold.interfaces.http_api.thumbnails import thumbnail_response
from shelfhold.library.public import RenditionService, ThumbnailService, cover_hash
from shelfhold.reading.application.page_stream import PageStream, StreamBudget

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
    "Cache-Control": "private,no-cache",
    "Vary": "Authorization",
    "X-Content-Type-Options": "nosniff",
}


class AtomFeed:
    """The OPDS 1.2 way of writing a feed (see `feed.FeedFormat`)."""

    op_prefix = "opds"
    mount = ""
    root_media = FEED_TYPE

    def media(self, kind: Kind) -> str:
        return feed_type(kind)

    def render(self, doc: FeedDoc) -> str:
        return build_feed(
            feed_id=doc.feed_id,
            title=doc.title,
            updated=doc.updated,
            kind=doc.kind,
            links=doc.links,
            entries=doc.entries,
            total=doc.total,
            start=doc.start,
            per_page=doc.per_page,
        )

    def paths(self, prefix: str) -> Paths:
        return Paths(prefix)

    def start_links(self, paths: Paths) -> list[Link]:
        return [
            Link("start", paths.root(), feed_type("navigation")),
            Link("search", paths.opensearch(), "application/opensearchdescription+xml"),
        ]


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

    def __init__(
        self, offset: int, limit: int, make: Callable[..., str], media: Callable[[Kind], str]
    ) -> None:
        self.offset, self.limit, self.make, self.media = offset, limit, make, media

    def _href(self, offset: int) -> str:
        return self.make(offset=offset, limit=None if self.limit == PAGE else self.limit)

    def links(self, *, has_next: bool, kind: Kind = "acquisition") -> list[Link]:
        kind_type = self.media(kind)
        found = [Link("self", self._href(self.offset), kind_type)]
        if self.offset > 0:
            found.append(Link("previous", self._href(max(0, self.offset - self.limit)), kind_type))
        if has_next:
            found.append(Link("next", self._href(self.offset + self.limit), kind_type))
        return found


async def _closing(chunks: Any) -> AsyncIterator[bytes]:
    """The bytes of a block, and the slot and descriptors given back the moment the response ends
    for any reason: done, failed, or the client gone (the task is cancelled, or the send fails).
    Starlette never closes a synchronous iterator it is handed."""
    try:
        async for piece in iterate_in_threadpool(chunks):
            yield piece
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def build_opds_router(
    get_engine: Callable[[], Engine],
    get_thumbnails: Callable[[], ThumbnailService],
    get_bundles: Callable[[], SeriesBundleProvider | None],
    now: Callable[[], datetime],
    get_streams: Callable[[], PageStream],
    get_renditions: Callable[[], RenditionService],
) -> APIRouter:
    router = APIRouter(prefix="/opds", tags=["opds"])
    add_pse_routes(router, get_streams=get_streams, get_renditions=get_renditions)

    def _stamp() -> str:
        return now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _lang(request: Request, principal: Principal) -> str:
        return language_from_headers(request.headers, principal)

    def _scope(connection: Connection, principal: Principal) -> ReaderScope:
        return reader_scope(connection, principal.user_id)

    def _add_feeds(fmt: FeedFormat) -> None:
        """The feed routes, written by one format. Both formats run this one body, so they ask
        the catalogue the same questions: there is no query of the JSON feed that is its own."""

        def _paths(request: Request) -> Paths:
            return fmt.paths(mount_prefix(request.scope))

        def _respond(doc: FeedDoc) -> Response:
            return Response(fmt.render(doc), media_type=fmt.media(doc.kind), headers=_HEADERS)

        def _start(paths: Paths) -> list[Link]:
            return fmt.start_links(paths)

        def _work_entries(works: list[VisibleWork], paths: Paths) -> list[Entry]:
            found = []
            for work in works:
                entry_links = [
                    Link(_SUBSECTION, paths.work(work.work_id), fmt.media("acquisition"))
                ]
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
            request: Request,
            principal: Principal,
            *,
            feed_id: str,
            title: str,
            make: Callable[..., str],
            offset: int,
            limit: int,
            recent: bool = False,
            shelf_id: str | None = None,
            series_id: str | None = None,
            evaluate: Callable[[Connection, ReaderScope], tuple[list[VisibleWork], int]]
            | None = None,
        ) -> Response:
            """`evaluate`: a saved selection (M5 L21) asks the page and the total itself, from the
            same one filter; nothing else changes."""
            paths = _paths(request)
            with get_engine().connect() as connection:
                scope = _scope(connection, principal)
                if evaluate is not None:
                    page, total = evaluate(connection, scope)
                else:
                    page = list_visible_works(
                        connection,
                        scope,
                        limit=limit + 1,
                        offset=offset,
                        recent=recent,
                        shelf_id=shelf_id,
                        series_id=series_id,
                    )
                    total = count_visible_works(
                        connection, scope, shelf_id=shelf_id, series_id=series_id
                    )
            shown = page[:limit]
            links = [
                *_Page(offset, limit, make, fmt.media).links(has_next=len(page) > limit),
                *_start(paths),
            ]
            links.append(Link("up", paths.root(), fmt.media("navigation")))
            body = FeedDoc(
                feed_id=feed_id,
                title=title,
                updated=_stamp(),
                kind="acquisition",
                links=links,
                entries=_work_entries(shown, paths),
                total=total,
                start=offset,
                per_page=limit,
            )
            return _respond(body)

        @router.get(
            fmt.mount,
            operation_id=f"{fmt.op_prefix}_root",
            response_class=Response,
            responses={200: {"content": {fmt.root_media: {}}, "description": "feed"}},
        )
        def root(request: Request, principal: Device) -> Response:
            paths = _paths(request)
            nav = fmt.media("navigation")
            lang = _lang(request, principal)
            acquisition = fmt.media("acquisition")
            door = [
                ("shelves", t_lang("opds.shelves", language=lang), paths.shelves(), nav),
                ("series", t_lang("opds.series", language=lang), paths.series_list(), nav),
                ("works", t_lang("opds.works", language=lang), paths.works(), acquisition),
                ("recent", t_lang("opds.recent", language=lang), paths.recent(), acquisition),
                ("selections", t_lang("opds.selections", language=lang), paths.selections(), nav),
            ]
            entries = [
                Entry(f"urn:shelfhold:opds:{key}", label, _stamp(), [Link(_SUBSECTION, href, kind)])
                for key, label, href, kind in door
            ]
            body = FeedDoc(
                feed_id="urn:shelfhold:opds:root",
                title=t_lang("opds.root_title", language=_lang(request, principal)),
                updated=_stamp(),
                kind="navigation",
                links=[Link("self", paths.root(), nav), *_start(paths)],
                entries=entries,
            )
            return _respond(body)

        def _listed_shelves(
            connection: Connection, scope: ReaderScope, user_id: str
        ) -> list[tuple[str, str]]:
            found = []
            for shelf in list_shelves_open_to(connection, user_id):
                if shelf_open_to(scope, shelf.max_age_rating) and count_visible_works(
                    connection, scope, shelf_id=shelf.id
                ):
                    found.append((shelf.id, shelf.name))
            return found

        @router.get(
            f"{fmt.mount}/shelves", operation_id=f"{fmt.op_prefix}_shelves", response_class=Response
        )
        def shelves(request: Request, principal: Device) -> Response:
            paths = _paths(request)
            with get_engine().connect() as connection:
                listed = _listed_shelves(
                    connection, _scope(connection, principal), principal.user_id
                )
            entries = [
                Entry(
                    f"urn:shelfhold:shelf:{shelf_id}",
                    name,
                    _stamp(),
                    [Link(_SUBSECTION, paths.shelf(shelf_id), fmt.media("acquisition"))],
                )
                for shelf_id, name in listed
            ]
            body = FeedDoc(
                feed_id="urn:shelfhold:opds:shelves",
                title=t_lang("opds.shelves", language=_lang(request, principal)),
                updated=_stamp(),
                kind="navigation",
                links=[Link("self", paths.shelves(), fmt.media("navigation")), *_start(paths)],
                entries=entries,
            )
            return _respond(body)

        @router.get(
            f"{fmt.mount}/shelves/{{shelf_id}}",
            operation_id=f"{fmt.op_prefix}_shelf",
            response_class=Response,
        )
        def shelf(
            shelf_id: str,
            request: Request,
            principal: Device,
            limit: Limit = PAGE,
            offset: Offset = 0,
        ) -> Response:
            _ulid(shelf_id)
            with get_engine().connect() as connection:
                listed = _listed_shelves(
                    connection, _scope(connection, principal), principal.user_id
                )
            name = dict(listed).get(shelf_id)
            if name is None:  # a shelf the reader is not shown is a shelf that does not exist
                raise _absent()
            return _works_feed(
                request,
                principal,
                feed_id=f"urn:shelfhold:shelf:{shelf_id}",
                title=name,
                make=lambda **q: _paths(request).shelf(shelf_id, **q),
                offset=offset,
                limit=limit,
                shelf_id=shelf_id,
            )

        @router.get(
            f"{fmt.mount}/series",
            operation_id=f"{fmt.op_prefix}_series_list",
            response_class=Response,
        )
        def series_list(
            request: Request, principal: Device, limit: Limit = PAGE, offset: Offset = 0
        ) -> Response:
            paths = _paths(request)
            with get_engine().connect() as connection:
                every = list_visible_series(connection, _scope(connection, principal))
            shown = every[offset : offset + limit]
            entries = [
                Entry(
                    f"urn:shelfhold:series:{series_id}",
                    title,
                    _stamp(),
                    [Link(_SUBSECTION, paths.series(series_id), fmt.media("acquisition"))],
                )
                for series_id, title in shown
            ]
            links = [
                *_Page(offset, limit, paths.series_list, fmt.media).links(
                    has_next=offset + limit < len(every), kind="navigation"
                ),
                *_start(paths),
            ]
            body = FeedDoc(
                feed_id="urn:shelfhold:opds:series",
                title=t_lang("opds.series", language=_lang(request, principal)),
                updated=_stamp(),
                kind="navigation",
                links=links,
                entries=entries,
                total=len(every),
                start=offset,
                per_page=limit,
            )
            return _respond(body)

        @router.get(
            f"{fmt.mount}/series/{{series_id}}",
            operation_id=f"{fmt.op_prefix}_series",
            response_class=Response,
        )
        def series(
            series_id: str,
            request: Request,
            principal: Device,
            limit: Limit = PAGE,
            offset: Offset = 0,
        ) -> Response:
            _ulid(series_id)
            with get_engine().connect() as connection:
                title = visible_series_title(connection, _scope(connection, principal), series_id)
            if title is None:
                raise _absent()
            return _works_feed(
                request,
                principal,
                feed_id=f"urn:shelfhold:series:{series_id}",
                title=title,
                make=lambda **q: _paths(request).series(series_id, **q),
                offset=offset,
                limit=limit,
                series_id=series_id,
            )

        @router.get(
            f"{fmt.mount}/works", operation_id=f"{fmt.op_prefix}_works", response_class=Response
        )
        def works(
            request: Request, principal: Device, limit: Limit = PAGE, offset: Offset = 0
        ) -> Response:
            return _works_feed(
                request,
                principal,
                feed_id="urn:shelfhold:opds:works",
                title=t_lang("opds.works", language=_lang(request, principal)),
                make=lambda **q: _paths(request).works(**q),
                offset=offset,
                limit=limit,
            )

        @router.get(
            f"{fmt.mount}/recent", operation_id=f"{fmt.op_prefix}_recent", response_class=Response
        )
        def recent(
            request: Request, principal: Device, limit: Limit = PAGE, offset: Offset = 0
        ) -> Response:
            return _works_feed(
                request,
                principal,
                feed_id="urn:shelfhold:opds:recent",
                title=t_lang("opds.recent", language=_lang(request, principal)),
                make=lambda **q: _paths(request).recent(**q),
                offset=offset,
                limit=limit,
                recent=True,
            )

        @router.get(
            f"{fmt.mount}/selections",
            operation_id=f"{fmt.op_prefix}_selections",
            response_class=Response,
        )
        def selections_list(request: Request, principal: Device) -> Response:
            """The saved selections of the profile of the token, and of nobody else (M5 L21): one
            entry each, an acquisition feed evaluated in her scope when it is opened."""
            paths = _paths(request)
            with get_engine().connect() as connection:
                viewer = SelectionViewer(principal.user_id, _scope(connection, principal))
            found = Selections(get_engine(), now).list_for(viewer)
            entries = [
                Entry(
                    f"urn:shelfhold:selection:{record.id}",
                    record.name,
                    _stamp(),
                    [Link(_SUBSECTION, paths.selection(record.id), fmt.media("acquisition"))],
                )
                for record in found
            ]
            body = FeedDoc(
                feed_id="urn:shelfhold:opds:selections",
                title=t_lang("opds.selections", language=_lang(request, principal)),
                updated=_stamp(),
                kind="navigation",
                links=[Link("self", paths.selections(), fmt.media("navigation")), *_start(paths)],
                entries=entries,
            )
            return _respond(body)

        @router.get(
            f"{fmt.mount}/selections/{{selection_id}}",
            operation_id=f"{fmt.op_prefix}_selection",
            response_class=Response,
        )
        def selection(
            selection_id: str,
            request: Request,
            principal: Device,
            limit: Limit = PAGE,
            offset: Offset = 0,
        ) -> Response:
            _ulid(selection_id)
            service = Selections(get_engine(), now)
            with get_engine().connect() as connection:
                viewer = SelectionViewer(principal.user_id, _scope(connection, principal))
            try:
                record = service.get(viewer, selection_id)
            except SelectionNotFoundError:  # another profile's selection is one that does not exist
                raise _absent() from None

            def evaluate(
                connection: Connection, scope: ReaderScope
            ) -> tuple[list[VisibleWork], int]:
                page = service.evaluate(
                    connection, SelectionViewer(principal.user_id, scope), record,
                    limit=limit + 1, offset=offset,
                )  # fmt: skip
                return page.works, page.total

            try:
                return _works_feed(
                    request,
                    principal,
                    feed_id=f"urn:shelfhold:selection:{selection_id}",
                    title=record.name,
                    make=lambda **q: _paths(request).selection(selection_id, **q),
                    offset=offset,
                    limit=limit,
                    evaluate=evaluate,
                )
            except SelectionTooSlowError:
                raise ProblemError("selection.too_slow") from None

        @router.get(
            f"{fmt.mount}/works/{{work_id}}",
            operation_id=f"{fmt.op_prefix}_work",
            response_class=Response,
        )
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
            lang = _lang(request, principal)
            for (
                edition
            ) in editions:  # only Editions the reader may see: the provider is asked for no other
                offers = (
                    provider.offers(
                        user_id=principal.user_id, scope=scope, edition_id=edition.edition_id
                    )
                    if provider is not None
                    else []
                )
                entry_links: list[Link] = []
                digest = cover_hash(edition.cover_ref) if edition.cover_ref else None
                if digest:
                    entry_links += [
                        Link(_THUMBNAIL, paths.cover(digest, 160), "image/webp"),
                        Link(_IMAGE, paths.cover(digest, 640), "image/webp"),
                    ]
                if not offers:
                    if len(editions) > 1:
                        label = t_lang(
                            "opds.complete_in", language=lang, title=title, lang=edition.lang
                        )
                    else:
                        label = t_lang("opds.complete", language=lang, title=title)
                    entries.append(
                        Entry(
                            f"urn:shelfhold:edition:{edition.edition_id}",
                            label,
                            _stamp(),
                            entry_links,
                        )
                    )
                    continue
                for offer in offers:  # a block over the cap is several entries: "(1 sur 3)", ...
                    notice = offer.notice or (
                        t_lang("opds.excluded", language=lang, count=offer.excluded)
                        if offer.excluded
                        else None
                    )
                    entries.append(
                        Entry(
                            f"urn:shelfhold:edition:{edition.edition_id}"
                            + (f":part:{offer.part}" if offer.part > 1 else ""),
                            safe_filename(offer.filename).rsplit(".", 1)[0],
                            _stamp(),
                            [
                                *entry_links,
                                Link(
                                    _ACQUISITION,
                                    paths.complete(edition.edition_id, offer.part),
                                    offer.media_type,
                                    length=offer.size,
                                ),
                            ],
                            summary=notice,
                        )
                    )
            budget = StreamBudget()  # one bound for the whole feed, not one per Edition
            for (
                edition
            ) in editions:  # a stream is offered for what the profile sees now, nothing else
                for stream in get_streams().offers(principal.user_id, edition.edition_id, budget):
                    entries.append(
                        Entry(
                            f"urn:shelfhold:stream:{stream.item_id}",
                            t_lang(
                                "opds.stream",
                                language=lang,
                                title=chr(32).join(filter(None, (title, stream.number))),
                            ),
                            _stamp(),
                            [
                                Link(
                                    STREAM_REL,
                                    paths.pse(stream.item_id),
                                    STREAM_TYPE,
                                    pse_count=stream.count,
                                )
                            ],
                        )
                    )
            body = FeedDoc(
                feed_id=f"urn:shelfhold:work:{work_id}",
                title=title,
                updated=_stamp(),
                kind="acquisition",
                links=[Link("self", paths.work(work_id), fmt.media("acquisition")), *_start(paths)],
                entries=entries,
            )
            return _respond(body)

        @router.get(
            f"{fmt.mount}/search", operation_id=f"{fmt.op_prefix}_search", response_class=Response
        )
        def search(
            request: Request,
            principal: Device,
            q: Annotated[str, Query(max_length=MAX_QUERY)] = "",
            limit: Limit = PAGE,
            offset: Offset = 0,
        ) -> Response:
            paths = _paths(request)
            words = q.strip()
            with get_engine().connect() as connection:
                found = search_page(
                    connection,
                    words,
                    limit=limit,
                    offset=offset,
                    scope=_scope(connection, principal),
                )
            works = [VisibleWork(h.work_id, h.title, h.kind) for h in found.hits]
            links = [
                *_Page(offset, limit, lambda **p: paths.search(words, **p), fmt.media).links(
                    has_next=offset + limit < found.total
                ),
                *_start(paths),
            ]
            body = FeedDoc(
                feed_id="urn:shelfhold:opds:search",
                title=t_lang("opds.results", language=_lang(request, principal)),
                updated=_stamp(),
                kind="acquisition",
                links=links,
                entries=_work_entries(works, paths),
                total=found.total,
                start=offset,
                per_page=limit,
            )
            return _respond(body)

    @router.get(
        "/opensearch.xml",
        operation_id="opds_opensearch",
        response_class=Response,
        responses={200: {"content": {"application/opensearchdescription+xml": {}}}},
    )
    def opensearch(request: Request, principal: Device) -> Response:
        body = opensearch_description(
            template=Paths(mount_prefix(request.scope)).search_template(),
            title=t_lang("opds.search_description", language=_lang(request, principal)),
        )
        return Response(body, media_type="application/opensearchdescription+xml", headers=_HEADERS)

    _add_feeds(AtomFeed())
    _add_feeds(JsonFeed())

    @router.get(
        "/covers/{content_hash}/{size}",
        operation_id="opds_cover",
        response_class=Response,
        responses={200: {"content": {"image/webp": {}}}, 304: {"description": "not-modified"}},
    )
    def cover(content_hash: str, size: str, request: Request, principal: Device) -> Response:
        return thumbnail_response(
            engine=get_engine(),
            service=get_thumbnails(),
            user_id=principal.user_id,
            content_hash=content_hash,
            size=size,
            if_none_match=request.headers.get("if-none-match"),
            vary="Authorization",
        )

    def _serve(download: BundleDownload) -> Response:
        headers = {
            **_HEADERS,
            "Content-Length": str(download.size),
            "Content-Disposition": attachment_disposition(safe_filename(download.filename)),
            "Content-Security-Policy": CSP_SANDBOX,
        }
        return StreamingResponse(
            _closing(download.chunks), media_type=download.media_type, headers=headers
        )

    def _asked(call: Callable[[], BundleDownload]) -> Response:
        """What a provider may say, as the answers of this feed: absent is 404 like any hidden
        thing, a block that cannot be served any more is 410 with no reason (a reason could name
        a title), too many blocks is 429 with an honest wait."""
        try:
            return _serve(call())
        except BundleNotFoundError:
            raise _absent() from None
        except BundleGoneError:
            raise ProblemError("bundle_gone", headers={"Cache-Control": "no-store"}) from None
        except BundleBusyError as busy:
            raise ProblemError(
                "bundle_busy",
                headers={"Retry-After": str(busy.retry_after_s), "Cache-Control": "no-store"},
            ) from None

    @router.get(
        "/editions/{edition_id}/complete",
        operation_id="opds_complete",
        response_class=Response,
        responses={
            200: {"content": {"application/vnd.comicbook+zip": {}}},
        },
    )
    def complete(
        edition_id: str, principal: Device, part: Annotated[int, Query(ge=1, le=999)] = 1
    ) -> Response:
        _ulid(edition_id)
        provider = get_bundles()
        if provider is None:  # nothing to serve is the answer of an absent thing
            raise _absent()
        with get_engine().connect() as connection:
            scope = _scope(connection, principal)
            visible = visible_edition_ids(connection, scope, [edition_id])
        if not visible:  # read now, not when the feed was: the URL is no capability
            raise _absent()
        return _asked(
            lambda: provider.open(
                user_id=principal.user_id,
                scope=scope,
                edition_id=edition_id,
                part=part,
                token_id=principal.key_id,
            )
        )

    @router.get(
        "/bundles/{version_id}",
        operation_id="opds_bundle_version",
        response_class=Response,
        responses={
            200: {"content": {"application/vnd.comicbook+zip": {}}},
        },
    )
    def bundle_version(version_id: str, principal: Device) -> Response:
        """A version made earlier, by its identifier: the profile's own only (any other is an
        absent one), and every chapter is checked again against the manifest and the scope."""
        _ulid(version_id)
        provider = get_bundles()
        opener = getattr(provider, "open_version", None)
        if opener is None:
            raise _absent()
        with get_engine().connect() as connection:
            scope = _scope(connection, principal)
        return _asked(
            lambda: opener(
                user_id=principal.user_id,
                scope=scope,
                version_id=version_id,
                token_id=principal.key_id,
            )
        )

    return router
