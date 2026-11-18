"""The pages of `/lite` (M5 L17a, EF-LEC-010, ENF-COMP-002, ENF-SEC-010, EF-USR-006).

Read-only, behind the cookie of `session`; the two forms (sign-in, sign-out) are the only verbs
other than GET. The one filter of the catalogue (M3 L09) is a condition of every query, with the
profile read from the database at each request (`reader_scope`): a page, a count, a search hit, a
cover and a download are made of what the profile may see, and nothing is filtered afterwards.
What is hidden, what does not exist and what is malformed are one answer: the same 404 page.

The download of a series goes the way the OPDS feed takes it (`LiteBundles` is the provider of
the block, the same one): a download is offered, and served, only for an Edition the reader may
see, read now; the file is the provider's (the neutralised derivative, never the original of a
source) and goes out with `attachment` and `sandbox`.
"""

import re
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import Connection, Engine
from starlette.concurrency import iterate_in_threadpool

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
from shelfhold.export.application.shelves import list_shelves_open_to
from shelfhold.identity.public import ThrottledError, reader_scope
from shelfhold.infrastructure.http_headers import CSP_SANDBOX, attachment_disposition
from shelfhold.interfaces.http_api.auth import gate_of
from shelfhold.interfaces.http_api.lite.render import (
    HEADERS,
    Page,
    fragment,
    page_response,
    render_fragment,
    t,
)
from shelfhold.interfaces.http_api.lite.session import (
    LiteRoute,
    Reader,
    absent,
    bare_page,
    cookie_names,
    csrf_refused,
    message,
    reader_of,
    redirect,
    require_tls,
    same_origin,
    set_cookie,
)
from shelfhold.interfaces.http_api.path_safety import safe_filename
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.ratelimit import client_ip_of
from shelfhold.interfaces.http_api.thumbnails import thumbnail_response
from shelfhold.library.public import ThumbnailService, cover_hash
from shelfhold.reading.bundle import (
    BundleBusyError,
    BundleDownload,
    BundleGoneError,
    BundleNotFoundError,
    BundleOffer,
)

__all__ = ["LiteBundles", "build_lite_router"]

PAGE = 25
MAX_OFFSET = 100_000
MAX_QUERY = 200
COVER = 160
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

Who = Annotated[Reader, Depends(reader_of)]
Offset = Annotated[int, Query(ge=0, le=MAX_OFFSET)]


class LiteBundles(Protocol):
    """The provider of the block of a series (the one of the OPDS feed): `scope` is the reader's."""

    def offers(self, *, user_id: str, scope: Any, edition_id: str) -> list[BundleOffer]: ...

    def open(
        self, *, user_id: str, scope: Any, edition_id: str, part: int = 1,
        token_id: str | None = None,
    ) -> BundleDownload: ...  # fmt: skip


def _ulid(who: Reader, raw: str) -> str:
    if not _ULID.fullmatch(raw):
        raise absent(who)
    return raw


def _row(href: str, label: str, cover: str | None = None) -> str:
    image = fragment("thumb", src=cover) if cover else ""
    return fragment("row", href=href, label=label, html_image=image)


async def _closing(chunks: Any) -> AsyncIterator[bytes]:
    """The bytes of a block, and the slot and descriptors given back the moment the response ends
    for any reason: done, failed, or the client gone. Starlette never closes a synchronous
    iterator it is handed."""
    try:
        async for piece in iterate_in_threadpool(chunks):
            yield piece
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def build_lite_router(
    get_engine: Callable[[], Engine],
    get_thumbnails: Callable[[], ThumbnailService],
    get_bundles: Callable[[], LiteBundles | None],
) -> APIRouter:
    router = APIRouter(
        prefix="/lite", tags=["lite"], route_class=LiteRoute, include_in_schema=False
    )

    def _scope(connection: Connection, who: Reader) -> ReaderScope:
        return reader_scope(connection, who.principal.user_id)

    def _pager(
        page: Page, make: Callable[[int], str], offset: int, *, has_next: bool
    ) -> dict[str, str]:
        previous = next_ = ""
        if offset > 0:
            href = make(max(0, offset - PAGE))
            previous = fragment("link", href=href, label=t("lite.previous", page))
        if has_next:
            next_ = fragment("link", href=make(offset + PAGE), label=t("lite.next", page))
        return {"html_previous": previous, "html_next": next_}

    def _listing(
        who: Reader,
        *,
        title: str,
        rows: list[str],
        make: Callable[[int], str] | None = None,
        offset: int = 0,
        has_next: bool = False,
        lead: str = "",
        query: str = "",
    ) -> Response:
        page = who.page
        empty = "" if rows else fragment("paragraph", text=t("lite.empty", page))
        pager = _pager(page, make, offset, has_next=has_next) if make else {
            "html_previous": "", "html_next": "",
        }  # fmt: skip
        body = render_fragment(
            "list",
            html_lead=lead, html_rows="".join(rows), html_empty=empty, **pager,
        )  # fmt: skip
        return page_response(page, title=title, body=body, query=query)

    def _work_rows(who: Reader, works: list[VisibleWork]) -> list[str]:
        paths, rows = who.page.paths, []
        for work in works:
            digest = cover_hash(work.cover_ref) if work.cover_ref else None
            rows.append(
                _row(
                    paths.work(work.work_id),
                    work.title,
                    paths.cover(digest, COVER) if digest else None,
                )
            )
        return rows

    def _works(
        who: Reader,
        *,
        title: str,
        make: Callable[[int], str],
        offset: int,
        recent: bool = False,
        shelf_id: str | None = None,
        series_id: str | None = None,
    ) -> Response:
        with get_engine().connect() as connection:
            found = list_visible_works(
                connection,
                _scope(connection, who),
                limit=PAGE + 1,
                offset=offset,
                recent=recent,
                shelf_id=shelf_id,
                series_id=series_id,
            )
        return _listing(
            who,
            title=title,
            rows=_work_rows(who, found[:PAGE]),
            make=make,
            offset=offset,
            has_next=len(found) > PAGE,
        )

    @router.get("", response_class=HTMLResponse)
    def home(who: Who) -> Response:
        page, paths = who.page, who.page.paths
        doors = (
            (paths.shelves(), t("lite.shelves", page)),
            (paths.series_list(), t("lite.series", page)),
            (paths.works(), t("lite.works", page)),
            (paths.recent(), t("lite.recent", page)),
        )
        return _listing(
            who, title=t("lite.root_title", page), rows=[_row(h, label) for h, label in doors]
        )

    # -- signing in and out ------------------------------------------------------------------

    def _form(request: Request, *, csrf: str, error: bool, status: int) -> Response:
        page = bare_page(request)
        body = render_fragment(
            "sign_in",
            lead=t("lite.sign_in_lead", page),
            html_error=fragment("alert", text=t("lite.sign_in_failed", page)) if error else "",
            action=page.paths.sign_in(),
            csrf=csrf,
            identifier=t("lite.identifier", page),
            secret=t("lite.secret", page),
            submit=t("lite.sign_in_submit", page),
        )
        return page_response(page, title=t("lite.sign_in_title", page), body=body, status=status)

    @router.get("/sign-in", response_class=HTMLResponse)
    def sign_in_form(request: Request, _tls: Annotated[None, Depends(require_tls)]) -> Response:
        nonce, token = gate_of(request).auth.lite_new_flow()
        response = _form(request, csrf=token, error=False, status=200)
        set_cookie(response, request, cookie_names(request).flow, nonce, max_age=3600)
        return response

    @router.post("/sign-in", response_class=HTMLResponse)
    def sign_in(
        request: Request,
        _tls: Annotated[None, Depends(require_tls)],
        identifier: Annotated[str, Form(max_length=64)] = "",
        secret: Annotated[str, Form(max_length=256)] = "",
        csrf: Annotated[str, Form(max_length=128)] = "",
    ) -> Response:
        same_origin(request)
        names, auth = cookie_names(request), gate_of(request).auth
        if not auth.lite_flow_ok(request.cookies.get(names.flow), csrf):
            raise csrf_refused(request)
        try:
            cookie = auth.lite_sign_in(identifier, secret, ip=client_ip_of(request.scope))
        except ThrottledError as throttled:
            page = bare_page(request)
            return message(
                page,
                title=t("lite.throttled_title", page),
                text=t("lite.throttled_text", page),
                status=429,
                href=page.paths.sign_in(),
                label=t("lite.retry", page),
                headers={"Retry-After": str(throttled.retry_after_s)},
            )
        if cookie is None:  # one answer for every failure; the form comes back with its own token
            return _form(request, csrf=csrf, error=True, status=401)
        response = redirect(request, bare_page(request).paths.home(), clear=(names.flow,))
        set_cookie(response, request, names.session, cookie, max_age=30 * 24 * 3600)
        return response

    @router.post("/sign-out", response_class=HTMLResponse)
    def sign_out(
        request: Request, who: Who, csrf: Annotated[str, Form(max_length=128)] = ""
    ) -> Response:
        same_origin(request)
        if not gate_of(request).auth.lite_csrf_ok(who.session, csrf):
            raise csrf_refused(request)
        return redirect(request, who.page.paths.sign_in(), clear=(cookie_names(request).session,))

    # -- browsing ---------------------------------------------------------------------------

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

    @router.get("/shelves", response_class=HTMLResponse)
    def shelves(who: Who) -> Response:
        with get_engine().connect() as connection:
            listed = _listed_shelves(connection, _scope(connection, who), who.principal.user_id)
        paths = who.page.paths
        rows = [_row(paths.shelf(shelf_id), name) for shelf_id, name in listed]
        return _listing(who, title=t("lite.shelves", who.page), rows=rows)

    @router.get("/shelves/{shelf_id}", response_class=HTMLResponse)
    def shelf(shelf_id: str, who: Who, offset: Offset = 0) -> Response:
        _ulid(who, shelf_id)
        with get_engine().connect() as connection:
            listed = _listed_shelves(connection, _scope(connection, who), who.principal.user_id)
        name = dict(listed).get(shelf_id)
        if name is None:  # a shelf the reader is not shown is a shelf that does not exist
            raise absent(who)
        return _works(
            who, title=name, make=lambda o: who.page.paths.shelf(shelf_id, o), offset=offset,
            shelf_id=shelf_id,
        )  # fmt: skip

    @router.get("/series", response_class=HTMLResponse)
    def series_list(who: Who, offset: Offset = 0) -> Response:
        with get_engine().connect() as connection:
            every = list_visible_series(connection, _scope(connection, who))
        paths = who.page.paths
        rows = [
            _row(paths.series(series_id), title)
            for series_id, title in every[offset : offset + PAGE]
        ]
        return _listing(
            who, title=t("lite.series", who.page), rows=rows, make=paths.series_list, offset=offset,
            has_next=offset + PAGE < len(every),
        )  # fmt: skip

    @router.get("/series/{series_id}", response_class=HTMLResponse)
    def series(series_id: str, who: Who, offset: Offset = 0) -> Response:
        _ulid(who, series_id)
        with get_engine().connect() as connection:
            title = visible_series_title(connection, _scope(connection, who), series_id)
        if title is None:
            raise absent(who)
        return _works(
            who, title=title, make=lambda o: who.page.paths.series(series_id, o), offset=offset,
            series_id=series_id,
        )  # fmt: skip

    @router.get("/works", response_class=HTMLResponse)
    def works(who: Who, offset: Offset = 0) -> Response:
        return _works(
            who, title=t("lite.works", who.page), make=who.page.paths.works, offset=offset
        )

    @router.get("/recent", response_class=HTMLResponse)
    def recent(who: Who, offset: Offset = 0) -> Response:
        return _works(
            who,
            title=t("lite.recent", who.page),
            make=who.page.paths.recent,
            offset=offset,
            recent=True,
        )

    @router.get("/search", response_class=HTMLResponse)
    def search(
        who: Who, q: Annotated[str, Query(max_length=MAX_QUERY)] = "", offset: Offset = 0
    ) -> Response:
        words = q.strip()
        works_found: list[VisibleWork] = []
        total = 0
        if words:
            with get_engine().connect() as connection:
                found = search_page(
                    connection, words, limit=PAGE, offset=offset, scope=_scope(connection, who)
                )
            works_found = [VisibleWork(h.work_id, h.title, h.kind) for h in found.hits]
            total = found.total
        return _listing(
            who, title=t("lite.results", who.page), rows=_work_rows(who, works_found),
            make=lambda o: who.page.paths.search(words, o), offset=offset,
            has_next=offset + PAGE < total, query=words,
        )  # fmt: skip

    @router.get("/works/{work_id}", response_class=HTMLResponse)
    def work(work_id: str, who: Who) -> Response:
        _ulid(who, work_id)
        provider = get_bundles()
        page, paths = who.page, who.page.paths
        with get_engine().connect() as connection:
            scope = _scope(connection, who)
            title = visible_work_title(connection, scope, work_id)
            editions = visible_editions_of_work(connection, scope, work_id) if title else []
        if title is None:
            raise absent(who)
        sections, offered = [], False
        for (
            edition
        ) in editions:  # only Editions the reader may see: the provider is asked for no other
            offers = (
                provider.offers(
                    user_id=who.principal.user_id, scope=scope, edition_id=edition.edition_id
                )
                if provider is not None
                else []
            )
            digest = cover_hash(edition.cover_ref) if edition.cover_ref else None
            heading = t("lite.edition_in", page, lang=edition.lang) if len(editions) > 1 else ""
            block = fragment("heading", text=heading) if heading else ""
            if digest:
                block += fragment("cover", src=paths.cover(digest, COVER))
            for offer in offers:  # a block over the cap is several: "(1 sur 3)", ...
                offered = True
                notice = offer.notice or (
                    t("lite.excluded", page, count=offer.excluded) if offer.excluded else ""
                )
                name = safe_filename(offer.filename).rsplit(".", 1)[0]
                label = (
                    t("lite.download_sized", page, mb=f"{offer.size / 1_000_000:.1f}")
                    if offer.size is not None
                    else t("lite.download", page)
                )
                href = paths.complete(edition.edition_id, offer.part)
                block += fragment("heading", text=name)
                block += fragment("note", text=notice) if notice else ""
                block += fragment("offer", href=href, label=label)
            sections.append(block)
        none = "" if offered else fragment("paragraph", text=t("lite.no_download", page))
        body = render_fragment("work", html_editions="".join(sections), html_none=none)
        return page_response(page, title=title, body=body)

    @router.get("/covers/{content_hash}/{size}")
    def cover(content_hash: str, size: str, request: Request, who: Who) -> Response:
        try:
            response = thumbnail_response(
                engine=get_engine(),
                service=get_thumbnails(),
                user_id=who.principal.user_id,
                content_hash=content_hash,
                size=size,
                if_none_match=request.headers.get("if-none-match"),
                vary="Cookie",
            )
        except ProblemError as problem:
            if problem.args[0] == "not_found":
                raise absent(who) from None
            raise
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- the download ------------------------------------------------------------------------

    def _serve(download: BundleDownload) -> Response:
        headers = {
            **HEADERS,
            "Content-Length": str(download.size),
            "Content-Disposition": attachment_disposition(safe_filename(download.filename)),
            "Content-Security-Policy": CSP_SANDBOX,
        }
        return StreamingResponse(
            _closing(download.chunks), media_type=download.media_type, headers=headers
        )

    @router.get("/editions/{edition_id}/complete")
    def complete(
        edition_id: str, who: Who, part: Annotated[int, Query(ge=1, le=999)] = 1
    ) -> Response:
        _ulid(who, edition_id)
        provider = get_bundles()
        if provider is None:  # nothing to serve is the answer of an absent thing
            raise absent(who)
        with get_engine().connect() as connection:
            scope = _scope(connection, who)
            visible = visible_edition_ids(connection, scope, [edition_id])
        if not visible:  # read now, not when the page was: the address is no capability
            raise absent(who)
        page = who.page
        try:
            return _serve(
                provider.open(
                    user_id=who.principal.user_id,
                    scope=scope,
                    edition_id=edition_id,
                    part=part,
                    token_id=who.principal.key_id,
                )
            )
        except BundleNotFoundError:
            raise absent(who) from None
        except BundleGoneError:  # no reason: a reason could name a title
            return message(
                page,
                title=t("lite.gone_title", page),
                text=t("lite.gone_text", page),
                status=410,
                href=page.paths.home(),
                label=t("lite.home", page),
            )
        except BundleBusyError as busy:
            return message(
                page,
                title=t("lite.busy_title", page),
                text=t("lite.busy_text", page),
                status=429,
                href=page.paths.home(),
                label=t("lite.home", page),
                headers={"Retry-After": str(busy.retry_after_s)},
            )

    @router.get("/{rest:path}", response_class=HTMLResponse)
    def anything_else(rest: str, request: Request, who: Who) -> Response:
        """No other address of `/lite` is a page (and none falls through to the front)."""
        if rest == "":
            return redirect(request, who.page.paths.home())
        raise absent(who)

    return router
