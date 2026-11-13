"""`/api/v1/requests`: ask for a book, follow the request, decide it (EF-USR-010, EF-USR-016).

Any profile asks and follows its own requests, by a session (a key never does): `POST` takes the
`request_ref` of a child's search, or (an adult) a Work, an ISBN or a text. A child has no other
door. A request of someone else is exactly as absent as one that never was (404, same body).

The admin's side: `GET /queue` (a private request of an adult comes without its title), `GET
/{id}/approval` (size, edition, shelf, whether an age is needed), `POST /{id}/approve` (the
reserved action `request.approve`: a fresh admin session holding the right to rate, never a key),
`POST /{id}/reject` (with a reason the requester reads) and `POST /{id}/not-found`.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.acquisition.application.requests import (
    ApprovalPreview,
    Requester,
    RequestRefusedError,
    RequestService,
    RequestView,
    SourceRef,
)
from shelfhold.acquisition.domain.request_target import RequestTarget
from shelfhold.events.family_state import FamilyState, request_family_state
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import require_requester, require_session, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops.errors import OpsError

__all__ = ["build_requests_router"]

Session = Annotated[Principal, Depends(require_session)]
RequesterOnly = Annotated[Principal, Depends(require_requester)]  # her session or her `request` key
_APPROVE = RESERVED_ACTIONS["request.approve"]
_MAX_REF = 64


def admin_session(principal: Session) -> Principal:
    if principal.role != "admin":
        raise ProblemError("forbidden")
    return principal


Admin = Annotated[Principal, Depends(admin_session)]
Approver = Annotated[Principal, Depends(reserved_action(_APPROVE, audit=False))]


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_ref: str | None = Field(default=None, max_length=_MAX_REF)
    target_kind: Literal["work_id", "source_ref", "isbn", "free_text"] | None = None
    target_value: str | None = Field(default=None, max_length=300)


class RequestOut(BaseModel):
    """What the person who asked sees: her own request, nothing about how it is handled."""

    id: str
    state: str
    title: str | None
    reject_reason: str | None
    created_at: datetime
    updated_at: datetime
    family_state: FamilyState | None  # the one word she reads (EF-UX-010); None: refused, withdrawn
    open_url: str | None  # "Ouvrir dans Kavita": read on the server, only when it is available

    @classmethod
    def of(cls, view: RequestView, kavita: Callable[[], str | None] = lambda: None) -> "RequestOut":
        state = request_family_state(view.state)
        return cls(
            id=view.id, state=view.state, title=view.title, reject_reason=view.reject_reason,
            created_at=view.created_at, updated_at=view.updated_at, family_state=state,
            open_url=kavita() if state == "available" else None,
        )  # fmt: skip


class RequestListOut(BaseModel):
    items: list[RequestOut]


class QueueItemOut(BaseModel):
    id: str
    title: str | None  # None: a private request of an adult
    target_kind: str
    visibility: str
    requester_id: str
    requester_role: str
    needs_age: bool
    created_at: datetime


class QueueOut(BaseModel):
    items: list[QueueItemOut]


class ApprovalOut(BaseModel):
    units: int
    size_bytes: int | None  # None: the source does not tell it
    units_minimum: bool  # a follow: the Units known now, more will come
    decided_age: int | None  # an age already decided for this series, to see before choosing
    decided_x: bool
    current_age: int | None  # the age the Work has now, when it is already rated
    current_x: bool
    needs_age: bool  # a child's request: "Choisir un âge"
    confirmation_required: bool  # over 100 Units or 5 GiB
    edition_id: str | None
    shelf_id: str | None
    resolved: bool

    @classmethod
    def of(cls, preview: ApprovalPreview) -> "ApprovalOut":
        return cls(**preview.__dict__)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    age: int | None = Field(default=None, ge=0, le=99)
    x: bool = False
    shelf_id: str | None = Field(default=None, min_length=26, max_length=26)
    confirm_size: bool = False


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=2000)


def build_requests_router(
    get_service: Callable[[], RequestService],
    resolve_ref: Callable[[str, str], SourceRef | None],
    kavita_url: Callable[[], str | None] = lambda: None,
) -> APIRouter:
    """`resolve_ref(token, owner)`: the remote result a child's search showed her, or None.
    `kavita_url()`: the address "Ouvrir dans Kavita" opens, read on the server (M3 L41)."""
    router = APIRouter(prefix="/api/v1/requests", tags=["requests"])

    def run[T](work: Callable[[RequestService], T]) -> T:
        try:
            return work(get_service())
        except RequestRefusedError as error:
            raise ProblemError(f"request.{error.code}") from None
        except OpsError as error:
            raise ProblemError(error.code, extensions=getattr(error, "extensions", None)) from None

    @router.post("", status_code=201)
    def create(body: RequestIn, principal: RequesterOnly) -> RequestOut:
        who = Requester(principal.user_id, principal.role)
        if body.request_ref is not None and body.target_kind is None:
            ref = resolve_ref(body.request_ref, principal.user_id)
            if ref is None:
                raise ProblemError("request.invalid_ref")
            return RequestOut.of(run(lambda s: s.create_from_ref(who, ref)))
        if body.target_kind is None or body.target_value is None or body.request_ref is not None:
            raise ProblemError("request.invalid_target")
        target = RequestTarget(body.target_kind, body.target_value)
        return RequestOut.of(run(lambda s: s.create(who, target)))

    @router.get("")
    def mine(principal: RequesterOnly) -> RequestListOut:
        return RequestListOut(
            items=[RequestOut.of(v, kavita_url) for v in run(lambda s: s.mine(principal.user_id))]
        )

    @router.get("/queue")
    def queue(principal: Admin) -> QueueOut:
        views = run(lambda s: s.queue(principal))
        return QueueOut(
            items=[
                QueueItemOut(
                    id=v.id,
                    title=v.title,
                    target_kind=v.target_kind,
                    visibility=v.visibility,
                    requester_id=v.requester_id,
                    requester_role=v.requester_role,
                    needs_age=v.needs_age,
                    created_at=v.created_at,
                )
                for v in views
            ]
        )

    @router.get("/{request_id}")
    def one(request_id: str, principal: RequesterOnly) -> RequestOut:
        return RequestOut.of(run(lambda s: s.get(principal.user_id, request_id)), kavita_url)

    @router.post("/{request_id}/cancel")
    def cancel(request_id: str, principal: RequesterOnly) -> RequestOut:
        return RequestOut.of(run(lambda s: s.cancel(principal.user_id, request_id)))

    @router.get("/{request_id}/approval")
    def approval(request_id: str, principal: Admin) -> ApprovalOut:
        return ApprovalOut.of(run(lambda s: s.preview(request_id, principal)))

    @router.post("/{request_id}/approve")
    def approve(request_id: str, body: ApproveIn, principal: Approver) -> RequestOut:
        view = run(
            lambda s: s.approve(
                request_id,
                principal,
                age=body.age,
                x=body.x,
                shelf_id=body.shelf_id,
                confirm_size=body.confirm_size,
            )
        )
        return RequestOut.of(view)

    @router.post("/{request_id}/reject")
    def reject(request_id: str, body: RejectIn, principal: Admin) -> RequestOut:
        return RequestOut.of(run(lambda s: s.reject(request_id, principal, body.reason)))

    @router.post("/{request_id}/not-found")
    def not_found(request_id: str, principal: Admin) -> RequestOut:
        return RequestOut.of(run(lambda s: s.mark_not_found(request_id, principal)))

    return router
