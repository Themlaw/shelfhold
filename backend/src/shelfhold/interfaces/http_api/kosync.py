"""The kosync routes a device token reaches (M4 L47, ADR-0053 §5 and §6, EF-LEC-009).

`GET /kosync/users/auth` is in `device_auth` (L46). Here are the three others, with the codes and
the shapes KOReader and the reference sync server speak (`api.json`, `KOSyncClient.lua`,
`koreader/koreader-sync-server`):

* `PUT /kosync/syncs/progress`: 200 `{document, timestamp}`, **always**, stored or not: for
  KOReader only a 200 is a success, and its queue stops at the first failure (a 202 or a 409 on one
  unknown file would hold every later position). Why a position was not taken (unknown or hidden
  digest, two files with one digest, a chapter that is gone, a guard) stays in the log and the
  counter; the answer is the same for all, so none of them is an oracle;
* `GET /kosync/syncs/progress/{document}`: 200 `{document, progress, percentage, device,
  device_id, timestamp}`, or 200 `{}` when nothing is known;
* `POST /kosync/users/create`: 402, always: the accounts are the profiles of Shelfhold and a device
  token is issued by the host.

Every route takes `require_device_kosync`: the credential, its revocation and its expiry are read
at each request (L46), TLS is required, and a wrong key is counted and limited. The profile is the
one of the token and nothing in the body or the headers can name another. The body is read only
after the token was accepted, and never beyond `MAX_BODY` bytes. What is wrong with a field is a
422 that names no value, for a body KOReader never sends (a field of the wrong type, a page that is
not a number); an unknown, a hidden and a malformed digest are one answer.
"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from shelfhold.i18n import negotiate, t_lang
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.device_auth import require_device_kosync
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.reading.public import InvalidProgressError, Kosync

__all__ = ["MAX_BODY", "build_kosync_router"]

MAX_BODY = 16 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
Device = Annotated[Principal, Depends(require_device_kosync)]


class ProgressBody(BaseModel):
    """What KOReader sends (`api.json`): `progress` is the page in text, or an xpointer."""

    document: str
    progress: str
    percentage: float
    device: str
    device_id: str | None = None
    metadata: dict[str, Any] | None = None


class ProgressStored(BaseModel):
    document: str
    timestamp: int


class ProgressRead(BaseModel):
    """A position, or nothing at all (`{}`): KOReader then says "no progress found"."""

    document: str | None = None
    progress: str | None = None
    percentage: float | None = None
    device: str | None = None
    device_id: str | None = None
    timestamp: int | None = None


def _invalid() -> ProblemError:
    return ProblemError("validation_failed", headers=_NO_STORE)


def _refuse_constant(_name: str) -> Any:
    raise ValueError  # NaN and Infinity are no JSON numbers


async def _body(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise ProblemError("request_too_large", headers=_NO_STORE)
    received = bytearray()
    async for part in request.stream():
        received += part
        if len(received) > MAX_BODY:
            raise ProblemError("request_too_large", headers=_NO_STORE)
    try:
        body = json.loads(bytes(received), parse_constant=_refuse_constant)
    except ValueError:
        raise _invalid() from None
    if not isinstance(body, dict):
        raise _invalid()
    return body


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise _invalid()
    return value


def _optional_text(body: dict[str, Any], key: str) -> str | None:
    return None if body.get(key) is None else _text(body, key)


def _progress(body: dict[str, Any]) -> str:
    """`tostring(progress)` is text; a number is accepted when it is whole."""
    value = body.get("progress")
    if isinstance(value, bool) or value is None:
        raise _invalid()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, str):
        return value
    raise _invalid()


def _percentage(body: dict[str, Any]) -> float:
    value = body.get("percentage")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _invalid()
    try:
        return float(value)
    except OverflowError:  # a JSON integer no float holds: the same wrong field, not a 500
        raise _invalid() from None


def build_kosync_router(service: Callable[[], Kosync], now: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/kosync", tags=["kosync"])

    def _announce(request: Request) -> None:
        request.app.state.kosync_unknown = service().unknown  # what Santé reads (ADR-0053 §5)

    def _token(request: Request) -> str:
        return request.headers.get("x-auth-user", "")[:64]

    @router.post(
        "/users/create",
        operation_id="kosync_register",
        status_code=402,
    )
    def register(request: Request) -> JSONResponse:
        """The "Register" button of KOReader: refused, whoever asks and whatever it sends. The
        device shows `message` (`doRegister`): in the language of the request, both when it names
        none (nobody is signed in to register)."""
        asked = request.headers.get("accept-language")
        languages = (negotiate(asked),) if asked else ("fr", "en")
        message = chr(10).join(
            t_lang("kosync.registration_disabled", language=x) for x in languages
        )
        raise ProblemError(
            "kosync_registration_disabled", extensions={"message": message}, headers=_NO_STORE
        )

    @router.put(
        "/syncs/progress",
        operation_id="kosync_update_progress",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ProgressBody.model_json_schema()}},
            }
        },
        responses={
            200: {"model": ProgressStored},
        },
    )
    async def update_progress(request: Request, principal: Device) -> JSONResponse:
        """Where the device is. The page arrives in text; a document the profile does not know,
        may not see or whose chapter is gone is accepted and nothing is stored."""
        body = await _body(request)
        document = _text(body, "document")
        progress = _progress(body)
        percentage = _percentage(body)
        device = _text(body, "device")
        device_id = _optional_text(body, "device_id")
        _announce(request)
        try:
            stored = await run_in_threadpool(
                lambda: service().push(
                    user_id=principal.user_id, token=_token(request), document=document[:256],
                    progress=progress, percentage=percentage, device=device, device_id=device_id,
                )
            )  # fmt: skip
        except InvalidProgressError:
            raise _invalid() from None
        del stored  # stored or not, the device is told the same: only a 200 is a success to it
        return JSONResponse(
            {"document": document, "timestamp": int(now().timestamp())}, headers=_NO_STORE
        )

    @router.get(
        "/syncs/progress/{document}",
        operation_id="kosync_get_progress",
        response_model=ProgressRead,
        response_model_exclude_none=True,
    )
    def get_progress(request: Request, document: str, principal: Device) -> JSONResponse:
        """The furthest position of the profile in this document, in its own pages, or `{}`."""
        _announce(request)
        found = service().pull(
            user_id=principal.user_id, document=document[:256], token=_token(request)
        )
        return JSONResponse({} if found is None else found.body(), headers=_NO_STORE)

    return router
