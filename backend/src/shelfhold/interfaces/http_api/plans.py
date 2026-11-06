"""`/api/v1/plans`: the preview (dry run) and its application (EF-EXP-008, EF-EXP-042).

A plan is applied with the `If-Match` of the preview it comes from; when anything moved since,
the answer is 412 `plan.stale` and the client asks for a new preview. Nothing is applied blind.
The application is a long operation: the route answers `202 Accepted` with the `Location` of the
job (`GET /api/v1/jobs/{id}`), the refusals that need no disk being answered before the job exists
(EF-INT-001).
"""

import re
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from shelfhold.export.application.export_items import SeriesCollisionError
from shelfhold.export.application.pipeline import (
    SourceMissingError,
    UnknownPlanError,
    UnverifiedParseError,
)
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.restore import RestoredBehindError
from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.identity.public import Credential, Principal
from shelfhold.interfaces.http_api.accepted import JobAccepted, accepted_job
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.preconditions import etag_for_version
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops import OpsError

__all__ = ["build_plans_router"]


class PlanPipeline(Protocol):
    def preview(self, shelf_destination_id: str) -> Any: ...
    def queue_apply(self, plan_id: str, *, if_match: str) -> str:
        """Queue the application of a stored plan; returns the identifier of the job."""
        ...


class PlanIn(BaseModel):
    shelf_destination_id: str


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
    is_admin_key = principal.credential is Credential.API_KEY and "admin" in principal.scopes
    if not (is_admin_session or is_admin_key):
        raise ProblemError("forbidden")
    return principal


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

    @router.post("/{plan_id}/apply", status_code=202, response_model=JobAccepted)
    def apply(
        plan_id: str,
        request: Request,
        _principal: Principal = Depends(require_admin),  # noqa: B008
    ) -> Response:
        if_match = _if_match_version(request.headers.get("if-match"))
        try:
            job_id = get_pipeline().queue_apply(plan_id, if_match=if_match)
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
