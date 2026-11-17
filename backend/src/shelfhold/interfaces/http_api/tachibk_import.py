"""`/api/v1/imports/tachibk`: import a Mihon backup, preview first (EF-INT-008, ENF-SEC-010).

All three routes are the one reserved action `import.tachibk`: a fresh admin session, never an API
key (the `admin` scope and a read-only one alike), never a device token, a member or a child. The
refusal comes before the body is read, so a file from anyone else is never decoded.

- `POST ?profile_id=` takes the file as the raw body (`application/octet-stream`, at most 16 MiB,
  counted as it arrives: a longer body is refused without being held). It is read as hostile, in a
  bounded subprocess, and what it would write is kept: the answer is the preview. Nothing reaches
  the library. `profile_id` is the profile that receives the read markers (default: the caller's).
- `GET /{id}` reads a preview or an applied batch;
- `POST /{id}/apply` writes it, once.

A refused file is a 422 with a closed `reason`, never a part of the file. An absent batch or profile
is a 404 with the same body whichever it is.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from shelfhold.acquisition.application.tachibk_import import (
    ApplyDone,
    ImportView,
    TachibkImportError,
    TachibkImportService,
    TachibkRefusedError,
)
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.platform.import_tachibk.decode import Limits

__all__ = ["build_tachibk_import_router"]

_ACTION = RESERVED_ACTIONS["import.tachibk"]
Importer = Annotated[Principal, Depends(reserved_action(_ACTION))]


class ItemOut(BaseModel):
    title: str
    source_key: str
    chapters: int
    read: int
    started: int
    trackers: int


class ImportOut(BaseModel):
    id: str
    profile_id: str
    schema_version: str
    state: str
    series: int
    chapters: int
    read: int  # chapters the file proves read
    started: int  # chapters it proves started (a page) and not finished
    trackers: int
    sources: int
    skipped: dict[str, int]
    items: list[ItemOut]

    @classmethod
    def of(cls, view: ImportView) -> "ImportOut":
        data = {k: v for k, v in view.__dict__.items() if k != "items"}
        return cls(**data, items=[ItemOut(**i.__dict__) for i in view.items])


class ApplyOut(BaseModel):
    id: str
    state: str
    pending_items: int
    markers: int
    hints: int

    @classmethod
    def of(cls, done: ApplyDone) -> "ApplyOut":
        return cls(**done.__dict__)


async def _body(request: Request, limit: int) -> bytes:
    """The raw body, at most `limit` bytes: it is counted as it arrives and refused past it."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise TachibkRefusedError("too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise TachibkRefusedError("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def build_tachibk_import_router(get_service: Callable[[], TachibkImportService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/imports/tachibk", tags=["imports"])

    def refusal(error: TachibkRefusedError | TachibkImportError) -> ProblemError:
        if isinstance(error, TachibkRefusedError):
            return ProblemError(error.code, extensions={"reason": error.reason})
        return ProblemError(error.code if error.code != "forbidden" else "forbidden")

    @router.post(
        "", status_code=201,
        openapi_extra={  # the file is the raw body, read as it arrives: the schema says so
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )  # fmt: skip
    async def preview_backup(
        request: Request, principal: Importer,
        profile_id: Annotated[str | None, Query(max_length=26)] = None,
    ) -> ImportOut:  # fmt: skip
        try:
            data = await _body(request, Limits().max_compressed)
            view = await run_in_threadpool(
                lambda: get_service().preview(principal, data, profile_id=profile_id)
            )
        except (TachibkRefusedError, TachibkImportError) as error:
            raise refusal(error) from None
        return ImportOut.of(view)

    @router.get("/{import_id}")
    def read_import(import_id: str, principal: Importer) -> ImportOut:
        try:
            return ImportOut.of(get_service().review(principal, import_id))
        except TachibkImportError as error:
            raise refusal(error) from None

    @router.post("/{import_id}/apply")
    def apply_import(import_id: str, principal: Importer) -> ApplyOut:
        try:
            return ApplyOut.of(get_service().apply(principal, import_id))
        except TachibkImportError as error:
            raise refusal(error) from None

    return router
