"""The door of `/lite`: TLS, the cookie, the profile read at each request, and the checks of its
two forms (M5 L17a, EF-LEC-010, ENF-SEC-010).

The one credential of `/lite` is a cookie that the secret of a device token opened. It is
limited to `{prefix}/lite` (`Path`, `HttpOnly`, `SameSite=Strict`, `Secure` over TLS), holds a
signed proof for one token and nothing of the secret, and reads nothing but pages: the only
verbs other than GET are the two forms of this package. At each request `LocalAuth.lite_resume`
reads the token again (live, its device not retired, its secret the one the session was made
with) and the profile is the token's subject, read from the database now: a role or a ceiling is
never kept. A session, an API key or a proxy identity opens nothing here (`auth.AuthMiddleware`
does not resolve them on this prefix), and the cookie opens nothing elsewhere.

Anything that stops a request (no session, a form refused, plain HTTP) is a `LiteStop` carrying
the page that answers: `LiteRoute` returns it, so a dependency can say "no" in HTML. A page that
needs a person is served only to one who is signed in; the sign-in page and its answers carry no
catalogue data.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import gate_of
from shelfhold.interfaces.http_api.lite.render import (
    HEADERS,
    Page,
    Paths,
    page_response,
    render_fragment,
    t,
)
from shelfhold.interfaces.http_api.path_safety import mount_prefix
from shelfhold.interfaces.http_api.problems import language_from_headers
from shelfhold.interfaces.http_api.ratelimit import client_scheme_of

SESSION_MAX_AGE_S = 30 * 24 * 3600
FLOW_MAX_AGE_S = 3600


class LiteStop(Exception):
    """Raise from a dependency or a route to answer with this page."""

    def __init__(self, response: Response) -> None:
        super().__init__("lite_stop")
        self.response = response


@dataclass(frozen=True)
class CookieNames:
    session: str
    flow: str  # binds the sign-in form to the browser that asked for it
    secure: bool


def cookie_names(request: Request) -> CookieNames:
    if client_scheme_of(request.scope) == "https":
        return CookieNames("__Secure-lite", "__Secure-lite-flow", secure=True)
    return CookieNames("lite", "lite-flow", secure=False)


def set_cookie(
    response: Response, request: Request, name: str, value: str, *, max_age: int
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=Paths(mount_prefix(request.scope)).base,
        secure=cookie_names(request).secure,
        httponly=True,
        samesite="strict",
    )


def clear_cookie(response: Response, request: Request, name: str) -> None:
    response.delete_cookie(
        name,
        path=Paths(mount_prefix(request.scope)).base,
        secure=cookie_names(request).secure,
        httponly=True,
        samesite="strict",
    )


def redirect(request: Request, href: str, *, clear: tuple[str, ...] = ()) -> Response:
    response = RedirectResponse(href, status_code=303, headers=HEADERS)
    for name in clear:
        clear_cookie(response, request, name)
    return response


def bare_page(request: Request) -> Page:
    """A page with no person behind it (the sign-in page, a refusal): no header, no token."""
    return Page(language_from_headers(request.headers), Paths(mount_prefix(request.scope)))


def message(
    page: Page,
    *,
    title: str,
    text: str,
    status: int,
    href: str,
    label: str,
    headers: dict[str, str] | None = None,
    retry_after_s: int | None = None,
    error: bool = False,
) -> Response:
    """A page that says one thing and gives the way out. A refusal (`error`) is framed twice;
    `retry_after_s`, when the door says how long to wait, is told in the page too."""
    if retry_after_s:
        text = f"{text} {t('lite.retry_after', page, seconds=retry_after_s)}"
    body = render_fragment(
        "message",
        message=text,
        href=href,
        label=label,
        html_kind=" lite__note--error" if error else "",
    )
    return page_response(page, title=title, body=body, status=status, headers=headers)


def absent_page(page: Page) -> Response:
    """The one 404 page of `/lite`: hidden, unknown and malformed are this page, byte for byte."""
    return message(
        page,
        title=t("lite.not_found_title", page),
        text=t("lite.not_found_text", page),
        status=404,
        href=page.paths.home(),
        label=t("lite.home", page),
    )


def absent(who: "Reader") -> LiteStop:
    return LiteStop(absent_page(who.page))


def require_tls(request: Request) -> None:
    """Before any secret is read: in clear it would travel, and replay, as it is (ADR-0053 §4)."""
    if client_scheme_of(request.scope) == "https" or getattr(
        request.app.state, "device_allow_plain_http", False
    ):
        return
    page = bare_page(request)
    raise LiteStop(
        message(
            page,
            title=t("lite.tls_title", page),
            text=t("lite.tls_text", page),
            status=403,
            href=page.paths.sign_in(),
            label=t("lite.retry", page),
            error=True,
        )
    )


def same_origin(request: Request) -> None:
    """A form is sent from this very site: a cross-site page cannot forge its `Origin`, and a
    browser that sends none is left to the token of the form. An opaque `Origin: null` is refused
    (a sandboxed or cross-site page sends it), except when the browser itself says the request is
    `same-origin` (`Sec-Fetch-Site`, which a page cannot set): Chrome sends `null` for a form posted
    under `Referrer-Policy: no-referrer`, which every page of `/lite` has."""
    headers = request.headers
    site = headers.get("sec-fetch-site", "").lower()
    if site == "cross-site":
        raise csrf_refused(request)
    origin, host = headers.get("origin"), headers.get("host", "")
    if origin == "null" and site == "same-origin":
        return
    if origin is not None and (
        not host or origin.split("://", 1)[-1].casefold() != host.casefold()
    ):
        raise csrf_refused(request)


def csrf_refused(request: Request) -> LiteStop:
    page = bare_page(request)
    return LiteStop(
        message(
            page,
            title=t("lite.csrf_title", page),
            text=t("lite.csrf_text", page),
            status=403,
            href=page.paths.sign_in(),
            label=t("lite.retry", page),
            error=True,
        )
    )


@dataclass(frozen=True)
class Reader:
    principal: Principal
    session: str
    page: Page


def reader_of(request: Request) -> Reader:
    """The signed-in profile of this request, or a redirect to the sign-in page."""
    require_tls(request)
    auth = gate_of(request).auth
    name = cookie_names(request).session
    value = request.cookies.get(name)
    principal = auth.lite_resume(value) if value else None
    paths = Paths(mount_prefix(request.scope))
    if principal is None or value is None:
        raise LiteStop(redirect(request, paths.sign_in(), clear=(name,) if value else ()))
    lang = language_from_headers(request.headers, principal)
    return Reader(principal, value, Page(lang, paths, auth.lite_csrf(value), principal.username))


class LiteRoute(APIRoute):
    """A route of `/lite`: what stops a request is a page, never a JSON problem."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except LiteStop as stop:
                return stop.response
            except RequestValidationError:  # a malformed address or query is an absent thing
                try:  # the page of the signed-in reader, as for a hidden Work (the door ran first)
                    page = (await run_in_threadpool(reader_of, request)).page
                except LiteStop:  # a form with no one behind it: the bare page
                    page = bare_page(request)
                return absent_page(page)

        return handler
