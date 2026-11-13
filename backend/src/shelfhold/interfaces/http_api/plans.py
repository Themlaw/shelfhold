"""`/api/v1/plans`: the preview (dry run) and its application (EF-EXP-008, EF-EXP-042).

A plan is applied with the `If-Match` of the preview it comes from; when anything moved since,
the answer is 412 `plan.stale` and the client asks for a new preview. Nothing is applied blind.
The application is a long operation: the route answers `202 Accepted` with the `Location` of the
job (`GET /api/v1/jobs/{id}`), the refusals that need no disk being answered before the job exists
(EF-INT-001).
"""

import re
import string
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from shelfhold.export.application.export_items import SeriesCollisionError
from shelfhold.export.application.naming_preview import NamingPreview
from shelfhold.export.application.pipeline import (
    SourceMissingError,
    UnknownPlanError,
    UnverifiedParseError,
)
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.rename_plan import (
    ItemMove,
    RenameConfirmationRequiredError,
    RenamePreview,
    RenameRefusedError,
)
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE, NamingTemplate
from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS
from shelfhold.identity.public import Credential, Principal
from shelfhold.interfaces.http_api.accepted import JobAccepted, accepted_job
from shelfhold.interfaces.http_api.auth import require_principal, reserved_action
from shelfhold.interfaces.http_api.preconditions import etag_for_version
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops import OpsError

__all__ = ["build_plans_router"]


class PlanPipeline(Protocol):
    def preview(self, shelf_destination_id: str) -> Any: ...
    def naming_preview(
        self, shelf_destination_id: str, template: NamingTemplate | None
    ) -> NamingPreview: ...
    def kind_of(self, plan_id: str) -> str:
        """`export`, `rename_plan`...; `UnknownPlanError` for a plan that is not there."""
        ...

    def rename_preview(
        self,
        destination_id: str,
        *,
        template: NamingTemplate | None = None,
        moves: tuple[ItemMove, ...] = (),
    ) -> RenamePreview: ...
    def queue_apply(
        self, plan_id: str, *, if_match: str, confirm_progress_loss: bool = False
    ) -> str:
        """Queue the application of a stored plan; returns the identifier of the job."""
        ...


class PlanIn(BaseModel):
    shelf_destination_id: str


class NamingTemplateIn(BaseModel):
    """The fields of a candidate template; what is left out is the default's."""

    model_config = ConfigDict(extra="forbid")
    chapter: str | None = None
    volume: str | None = None
    special: str | None = None
    special_dir: str | None = None
    segment: str | None = None


class NamingPreviewIn(BaseModel):
    shelf_destination_id: str
    template: NamingTemplateIn | None = None


class NameEntryOut(BaseModel):
    key: str
    rel_path: str
    template_ref: str
    status: str
    flags: list[str]


class CollisionOut(BaseModel):
    kind: str
    names: list[str]


class NamingPreviewOut(BaseModel):
    """`verified` false is the "not verified" mark: Kavita could not confirm the names."""

    verified: bool
    entries: list[NameEntryOut]
    collisions: list[CollisionOut]


_PLACEHOLDERS = {
    "chapter": {"series", "number"},
    "volume": {"series", "number"},
    "special": {"series", "label"},
    "segment": {"series", "n"},
}


def _candidate(body: NamingTemplateIn | None) -> NamingTemplate | None:
    """A template made of the default and the fields given; a placeholder it does not know is
    a 422, since the rendering would fail on it."""
    if body is None:
        return None
    fields = body.model_dump(exclude_none=True)
    for name, text in fields.items():
        try:
            used = {field for _lit, field, _spec, _conv in string.Formatter().parse(text) if field}
        except ValueError:
            raise ProblemError("validation_failed") from None
        if name in _PLACEHOLDERS and not used <= _PLACEHOLDERS[name]:
            raise ProblemError("validation_failed")
    return replace(DEFAULT_TEMPLATE, template_id="preview", version=1, **fields)


class ItemMoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    export_item_id: str = Field(min_length=26, max_length=26)
    new_rel_path: str = Field(min_length=1, max_length=2048)


class RenamePlanIn(BaseModel):
    """Either a template (the fields given are the candidate's, the rest the default's) or a list
    of moves (reshelving, binding migration): one of the two, never both, never neither."""

    model_config = ConfigDict(extra="forbid")
    shelf_destination_id: str
    template: NamingTemplateIn | None = None
    moves: list[ItemMoveIn] | None = Field(default=None, max_length=5000)


class ApplyIn(BaseModel):
    """The confirmation that readers lose their progress (EF-EXP-037); nothing else."""

    model_config = ConfigDict(extra="forbid")
    confirm_progress_loss: StrictBool = False


class RenameStepOut(BaseModel):
    kind: str
    src: str
    dst: str
    export_item_id: str | None


class RenamePlanOut(BaseModel):
    plan_id: str | None
    base_version: str
    steps: list[RenameStepOut]
    batches: int
    blocked: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


class PlanOut(BaseModel):
    plan_id: str
    base_version: str
    ops: list[dict[str, Any]]
    blocked: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    checks: list[str]


_STRONG_ETAG = re.compile(r'"[0-9a-f]{1,64}"')


def _if_match_version(header: str | None) -> str:
    """The strong validator of the preview, quoted: not `W/"..."`, not bare, not `*` (a plan is
    applied only by someone who saw its version) and not a list."""
    if header is None:
        raise ProblemError("precondition_required")
    text = header.strip()
    if _STRONG_ETAG.fullmatch(text) is None:
        raise ProblemError("precondition_failed")
    return text[1:-1]


def require_admin(request: Request) -> Principal:
    """Plans touch the library on disk: an admin session or an API key with the `admin` scope."""
    principal = require_principal(request)
    is_admin_session = principal.credential is Credential.SESSION and principal.role == "admin"
    is_admin_key = (
        principal.credential is Credential.API_KEY
        and principal.role == "admin"
        and "admin" in principal.scopes
    )
    if not (is_admin_session or is_admin_key):
        raise ProblemError("forbidden")
    return principal


_RENAME_APPLY = reserved_action(RESERVED_ACTIONS["plan.apply_rename"])


def build_plans_router(get_pipeline: Callable[[], PlanPipeline]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/plans", tags=["plans"])

    @router.post("", status_code=201)
    def preview(
        body: PlanIn,
        response: Response,
        _principal: Principal = Depends(require_admin),  # noqa: B008
    ) -> PlanOut:
        result = get_pipeline().preview(body.shelf_destination_id)
        response.headers["ETag"] = etag_for_version(result.base_version)
        return PlanOut(
            plan_id=result.plan_id,
            base_version=result.base_version,
            ops=[asdict(op) for op in result.ops],
            blocked=[asdict(b) for b in result.blocked],
            warnings=[asdict(w) for w in result.warnings],
            checks=list(result.checks),
        )

    @router.post("/renames", status_code=200)
    def rename_preview(
        body: RenamePlanIn,
        response: Response,
        _principal: Principal = Depends(require_admin),  # noqa: B008
    ) -> RenamePlanOut:
        """The plan of renames a template change, a reshelving or a binding migration makes, with
        "the Kavita progress of N readers will be lost" when it applies (EF-EXP-030, EF-EXP-037).
        The count is for the admin who asks: nobody else reaches this door."""
        if (body.template is None) == (not body.moves):
            raise ProblemError("validation_failed")
        template = _candidate(body.template)
        moves = tuple(ItemMove(m.export_item_id, m.new_rel_path) for m in body.moves or ())
        try:
            result = get_pipeline().rename_preview(
                body.shelf_destination_id, template=template, moves=moves
            )
        except RenameRefusedError as error:
            raise ProblemError(error.code, extensions={"reason": error.reason}) from None
        except (DestinationPausedError, RestoredBehindError, OpsError) as error:
            raise ProblemError(error.code) from None
        if result.plan_id is not None:
            response.headers["ETag"] = etag_for_version(result.base_version)
        return RenamePlanOut(
            plan_id=result.plan_id,
            base_version=result.base_version,
            steps=[RenameStepOut(**asdict(s)) for s in result.steps],
            batches=result.batches,
            blocked=list(result.blocked),
            warnings=[asdict(w) for w in result.warnings],
        )

    @router.post("/naming-preview", status_code=200)
    def naming_preview(
        body: NamingPreviewIn,
        _principal: Principal = Depends(require_admin),  # noqa: B008
    ) -> NamingPreviewOut:
        """The names a template would give, read by Kavita or marked "not verified" (EF-EXP-005).
        Read-only: nothing is written and nothing is stored."""
        result = get_pipeline().naming_preview(body.shelf_destination_id, _candidate(body.template))
        return NamingPreviewOut(
            verified=result.verified,
            entries=[NameEntryOut(**{**asdict(e), "flags": list(e.flags)}) for e in result.entries],
            collisions=[CollisionOut(kind=c.kind, names=list(c.names)) for c in result.collisions],
        )

    @router.post("/{plan_id}/apply", status_code=202, response_model=JobAccepted)
    def apply(
        plan_id: str,
        request: Request,
        body: ApplyIn | None = None,
        _principal: Principal = Depends(require_admin),  # noqa: B008
    ) -> Response:
        pipeline = get_pipeline()
        try:
            kind = pipeline.kind_of(plan_id)
        except UnknownPlanError:
            raise ProblemError("not_found") from None
        if kind == "rename_plan":
            _RENAME_APPLY(request)  # a fresh admin session; never a key, a member or a child
        if_match = _if_match_version(request.headers.get("if-match"))
        confirmed = body is not None and body.confirm_progress_loss
        try:
            if kind == "rename_plan":
                job_id = pipeline.queue_apply(
                    plan_id, if_match=if_match, confirm_progress_loss=confirmed
                )
            else:
                job_id = pipeline.queue_apply(plan_id, if_match=if_match)
        except RenameConfirmationRequiredError as error:
            raise ProblemError(
                error.code, extensions={"readers": error.readers, "exact": error.exact}
            ) from None
        except RenameRefusedError as error:
            raise ProblemError(error.code, extensions={"reason": error.reason}) from None
        except StalePlanError:
            raise ProblemError("plan.stale") from None
        except UnknownPlanError:
            raise ProblemError("not_found") from None
        except (
            SeriesCollisionError,
            SourceMissingError,
            UnverifiedParseError,
            DestinationPausedError,
            RestoredBehindError,
            OpsError,
        ) as error:
            raise ProblemError(error.code) from None
        return accepted_job(job_id)

    return router
