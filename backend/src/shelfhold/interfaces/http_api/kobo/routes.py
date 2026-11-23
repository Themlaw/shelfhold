"""The Kobo sync routes (M6 L17a, EF-INT-012, D20, Q21 case (b) of D-P5).

The protocol is not documented by the manufacturer: the surface is the minimal one the device
needs, written by hand from the form its requests have. Three routes answer for real — the
initialization (the resources of this server and of no other: the store proxy is off, no request
is relayed, none leaves), the dummy activation of `v1/auth/device` (accepted, nothing stored: the
device is already paired, its token is the path segment) and the library sync, page by page
(`x-kobo-synctoken` to continue, `X-Kobo-sync: continue` while a page follows). Every other
route under the token has one generic answer, `{}`, the same for every method: the device asks,
the answer says "nothing to do", and no undocumented route becomes an error the device stops on.
The download and the cover of a book are the exception: the metadata names them, the bytes are
L17b's, and they answer 404 until that lot lands — an empty 200 would tell the device it holds
a file it has not.

Every route takes `require_device_kobo`: the credential is the path segment, TLS is required, a
wrong guess is counted and limited, and the profile — the scope of every query — is the token's,
read from the database at each request. The links the answers carry (the resources of the
initialization, the download and the cover of a book) are relative and begin at the mount point
(`path_safety.mount_prefix`): the address the device already holds, never one built from `Host`.
Nothing is cached: every answer says `Cache-Control: no-store`, for a library that changes with
a ceiling, a shelf or a hiding.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.device_auth import require_device_kobo
from shelfhold.interfaces.http_api.path_safety import mount_prefix
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.reading.public import KoboSync

__all__ = ["build_kobo_router"]

Device = Annotated[Principal, Depends(require_device_kobo)]
_NO_STORE = "no-store"
_STUB_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def build_kobo_router(service: Callable[[], KoboSync]) -> APIRouter:
    router = APIRouter(prefix="/kobo", tags=["kobo"])

    def _base(request: Request, token: str) -> str:
        """Where the device already is: the mount point, the door, the segment it was given."""
        return f"{mount_prefix(request.scope)}/kobo/{token}"

    @router.get("/{token}/v1/initialization", operation_id="kobo_initialization")
    def initialization(
        request: Request, response: Response, token: str, principal: Device
    ) -> dict[str, Any]:
        """The resources of this server and of no other: the three routes of the sync, at the
        address the device already holds, and the profile of the token."""
        response.headers["Cache-Control"] = _NO_STORE
        return service().initialization(principal=principal, base=_base(request, token))

    @router.post("/{token}/v1/auth/device", operation_id="kobo_auth_device")
    def auth_device(response: Response, token: str, _principal: Device) -> dict[str, Any]:
        """The activation the device asks for at each wake: accepted, nothing stored — the
        device is already paired, its credential is the path it asked on."""
        response.headers["Cache-Control"] = _NO_STORE
        return {}

    @router.get("/{token}/v1/library/sync", operation_id="kobo_library_sync")
    def library_sync(
        request: Request, response: Response, token: str, principal: Device
    ) -> JSONResponse:
        """One page of the sync of this device: the entitlements, the tags and the reading
        states it is given, the token of the next page, and whether one follows."""
        page = service().sync(
            principal=principal,
            token_id=principal.key_id,
            base=_base(request, token),
            continuation=request.headers.get("x-kobo-synctoken"),
        )
        headers = {"Cache-Control": _NO_STORE, "x-kobo-synctoken": page.continuation}
        if page.continue_sync:
            headers["x-kobo-sync"] = "continue"
        return JSONResponse(page.body, headers=headers)

    @router.api_route(
        "/{token}/{rest:path}",
        methods=list(_STUB_METHODS),
        include_in_schema=False,
    )
    def stub(response: Response, token: str, rest: str, _principal: Device) -> dict[str, Any]:
        """Every other route under the token: one generic answer, the same for every method —
        no undocumented route becomes an error the device stops its sync on. The download and
        the cover are the exception: the metadata names them, the bytes are L17b's, and an
        empty 200 would tell the device it holds a file it has not — they answer 404 until
        the lot that serves them lands."""
        response.headers["Cache-Control"] = _NO_STORE
        if rest.startswith(("v1/download/", "v1/covers/")):
            raise ProblemError("not_found")
        return {}

    return router
