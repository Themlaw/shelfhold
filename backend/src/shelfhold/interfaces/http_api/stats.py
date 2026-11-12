"""`GET /api/v1/stats?since=30d`: value indicators of the instance (EF-INT-025).

Computed locally from existing tables, readable by the admin only: a member, a child or a key of
either is refused (403) before anything is read. Never sent anywhere (ENF-PRIV-001).
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Engine

from shelfhold.identity.public import Principal
from shelfhold.infrastructure.stats import (
    DEFAULT_SINCE,
    InvalidSinceError,
    compute_stats,
    parse_since,
)
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_stats_router"]


class HumanInterventions(BaseModel):
    waiting_states: int
    needs_review_replays: int


class GuardsTriggered(BaseModel):
    sentinel: int
    foreign_writer: int


class ProfileRequests(BaseModel):
    profile_id: str
    username: str
    requested: int
    delivered: int
    delivered_share_percent: int


class StatsOut(BaseModel):
    since_days: int
    human_interventions: HumanInterventions
    units_exported: int
    median_detection_to_visible_s: int | None
    arrivals_pending: int
    guards_triggered: GuardsTriggered
    requests_by_profile: list[ProfileRequests]


def require_admin(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    if principal.role != "admin":
        raise ProblemError("forbidden")
    return principal


def build_stats_router(get_engine: Callable[[], Engine], now: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["stats"])

    @router.get("/stats", response_model=StatsOut)
    def stats(
        _admin: Annotated[Principal, Depends(require_admin)],
        since: Annotated[str, Query()] = DEFAULT_SINCE,
    ) -> StatsOut:
        try:
            days = parse_since(since)
        except InvalidSinceError as error:
            raise ProblemError("validation_failed") from error
        return StatsOut.model_validate(
            compute_stats(get_engine(), now=now(), since_days=days).as_dict()
        )

    return router
