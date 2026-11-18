"""`GET /content/{ticket}/{path}`: the bytes of a book (M5 L05a, EF-LEC-005, ENF-SEC-004, ADR-0055).

The only door for an octet of a book. The credential is the ticket, in the path, bound to the
profile, the carrier, the scope and the session; the cookie of a session is what says who asks
(on a separate origin, `SHELFHOLD_CONTENT_ORIGIN`, no cookie is sent and the ticket's session
fingerprint is checked against the live sessions of the profile). A key of the API and a device
token are refused. Everything that is refused answers one 404 (a forged or expired ticket, a profile
that may not see, an entry outside the list, a name that climbs): no oracle, no tag, no length.
The authorisation is complete before the `ETag` is compared or a byte is read for it.
"""

from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import Response

from shelfhold.identity.public import Credential
from shelfhold.infrastructure.http_headers import PRIVATE_CACHE
from shelfhold.interfaces.http_api.auth import current_principal
from shelfhold.interfaces.http_api.content import content_response
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.reading.application.content_access import ContentAccess, ContentRefusedError

__all__ = ["build_content_router"]

_ABSENT = {"Cache-Control": "no-store"}


def _matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    if header.strip() == "*":
        return True
    return any(tag.strip().removeprefix("W/") == etag for tag in header.split(","))


def build_content_router(
    get_access: Callable[[], ContentAccess], *, allowed_origin: str | None
) -> APIRouter:
    router = APIRouter(tags=["content"])

    def cors(request: Request, response: Response) -> None:
        """Only the origin of the SPA may read the bytes from the separate origin, and never with
        its credentials (there are none to send)."""
        origin = request.headers.get("origin")
        if allowed_origin is not None and origin == allowed_origin:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
            response.headers["Vary"] = "Origin"

    @router.options("/content/{ticket}/{path:path}", include_in_schema=False)
    def preflight(ticket: str, path: str, request: Request) -> Response:
        response = Response(status_code=204)
        if allowed_origin is not None and request.headers.get("origin") == allowed_origin:
            cors(request, response)
            response.headers["Access-Control-Allow-Methods"] = "GET"
            response.headers["Access-Control-Allow-Headers"] = "If-None-Match"
            response.headers["Access-Control-Max-Age"] = "600"
        return response

    @router.get(
        "/content/{ticket}/{path:path}",
        response_class=Response,
        responses={
            200: {"content": {"application/octet-stream": {}}, "description": "entry"},
            304: {"description": "not-modified"},
            404: {"description": "not-found"},
        },
    )
    def content(ticket: str, path: str, request: Request) -> Response:
        principal = current_principal(request)
        if principal is not None and principal.credential is not Credential.SESSION:
            raise ProblemError("credential_not_allowed")  # a key, a device token: never here
        session = (
            (principal.user_id, principal.session_id)
            if principal is not None
            and principal.session_id is not None
            and not principal.via_forward
            else None
        )
        try:
            served = get_access().read(ticket, path, session=session)
        except ContentRefusedError:
            raise ProblemError("not_found", headers=_ABSENT) from None
        if _matches(request.headers.get("if-none-match"), served.etag):
            response: Response = Response(status_code=304, headers={"ETag": served.etag})
            response.headers["Cache-Control"] = PRIVATE_CACHE
        else:
            response = content_response(
                served.body,
                media_type=served.media_type,
                filename=served.filename,
                etag=served.etag,
            )
        cors(request, response)
        return response

    return router
