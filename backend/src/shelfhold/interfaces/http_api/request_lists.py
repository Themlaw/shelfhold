"""`/api/v1/request-lists`: import a list of wanted books, read it on one screen (EF-USR-020).

`POST` takes the text of the list (one title per line, an ISBN if there is one) and makes the
caller's own reading shelf and requests: an adult's, by a session (a key never imports, whatever
its scope; a child never does: she asks among her search results). `POST /for-child/{id}` is the
admin's, for a child: the reserved action `reading_shelf.attach` (a fresh admin session, never a
key). Nothing is searched in a source, for anyone. `GET /{id}` is the one screen of the batch (the
admin's, or the owner's for their own); a batch of someone else is exactly an absent one.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.acquisition.application.request_lists import (
    ImportDone,
    InvalidListError,
    ListReview,
    ListSummary,
    RequestListService,
)
from shelfhold.acquisition.application.requests import RequestRefusedError
from shelfhold.acquisition.domain.request_list import MAX_LIST_BYTES
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import require_session, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_request_lists_router"]

Session = Annotated[Principal, Depends(require_session)]
Attacher = Annotated[Principal, Depends(reserved_action(RESERVED_ACTIONS["reading_shelf.attach"]))]


class ListIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # the text of the list; the cap in bytes is the parser's, this one only keeps a body small
    text: str = Field(max_length=MAX_LIST_BYTES)
    name: str | None = Field(default=None, max_length=200)


class ListImportOut(BaseModel):
    id: str
    in_library: int  # lines put on the reading shelf
    requested: int  # lines that became a request
    annotated: int  # lines that ask for a precise annotated edition: a warning, not a refusal
    collection_id: str | None

    @classmethod
    def of(cls, done: ImportDone) -> "ListImportOut":
        return cls(**done.__dict__)


class ListItemOut(BaseModel):
    position: int
    text: str | None  # None: another adult's list, shown to an admin as counts only
    isbn: str | None
    outcome: str
    annotated: bool
    request_id: str | None
    request_state: str | None
    needs_age: bool  # a child's line: an age is chosen at approval
    in_library: bool


class ListReviewOut(BaseModel):
    id: str
    name: str
    user_id: str
    user_role: str
    created_at: datetime
    collection_id: str | None
    in_library: int
    requested: int
    annotated: int
    items: list[ListItemOut]

    @classmethod
    def of(cls, review: ListReview) -> "ListReviewOut":
        data = {k: v for k, v in review.__dict__.items() if k != "items"}
        return cls(**data, items=[ListItemOut(**i.__dict__) for i in review.items])


class ListSummaryOut(BaseModel):
    id: str
    name: str
    user_id: str
    user_role: str
    created_at: datetime
    requested: int
    in_library: int

    @classmethod
    def of(cls, summary: ListSummary) -> "ListSummaryOut":
        return cls(**summary.__dict__)


class ListSummariesOut(BaseModel):
    items: list[ListSummaryOut]


def build_request_lists_router(get_service: Callable[[], RequestListService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/request-lists", tags=["requests"])

    def run[T](work: Callable[[RequestListService], T]) -> T:
        try:
            return work(get_service())
        except InvalidListError as refused:
            raise ProblemError(
                "request.invalid_list", extensions={"reason": refused.reason}
            ) from None
        except RequestRefusedError as error:
            raise ProblemError(f"request.{error.code}") from None

    @router.post("", status_code=201)
    def import_request_list(body: ListIn, principal: Session) -> ListImportOut:
        return ListImportOut.of(run(lambda s: s.import_list(principal, body.text, name=body.name)))

    @router.post("/for-child/{user_id}", status_code=201)
    def import_request_list_for_child(
        user_id: str, body: ListIn, principal: Attacher
    ) -> ListImportOut:
        return ListImportOut.of(
            run(lambda s: s.import_list(principal, body.text, name=body.name, for_user_id=user_id))
        )

    @router.get("")
    def list_request_lists(
        principal: Session, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> ListSummariesOut:
        found = run(lambda s: s.imports(principal, limit=limit))
        return ListSummariesOut(items=[ListSummaryOut.of(i) for i in found])

    @router.get("/{import_id}")
    def read_request_list(import_id: str, principal: Session) -> ListReviewOut:
        return ListReviewOut.of(run(lambda s: s.review(principal, import_id)))

    return router
