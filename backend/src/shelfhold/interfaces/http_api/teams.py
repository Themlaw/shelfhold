"""`/api/v1/editions/{id}/team-switch`: replace what is served by the preferred team's (M4 L35).

Choosing a team on the series sheet only orders the teams (`preferred-team`): nothing already
served changes. This is the second step, and it is reserved (`team.switch`, ENF-SEC-010): a fresh
admin session, never a key (the `admin` scope and a read-only one alike), a member or a child.

- `POST` reads and keeps the preview: the Units that would be switched, the blocked ones with the
  closed word of what refuses them, the pending ones. It writes nothing to the library;
- `POST /{switch}/apply` queues the application (202 and the `job_id`): a write job, which takes
  the lease of the library, replaces each served file under an intent, and keeps the report. A
  preview gone stale is not applied;
- `GET /{switch}` reads the report (the screen `SwitchReport`).

An edition or a switch that does not exist, or that belongs to another series, is one answer: 404
`team_switch.not_found`, the identifier asked for never echoed.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from shelfhold.acquisition.application.team_switch import (
    TeamSwitchRefusedError,
    TeamSwitchService,
)
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.accepted import JobAccepted, accepted_job
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_teams_router"]

# The service writes its own audit lines, with the switch it concerns: the guard writes none (a
# refused call, a preview or a report changes nothing).
_SWITCH = reserved_action(RESERVED_ACTIONS["team.switch"], audit=False)
Switcher = Annotated[Principal, Depends(_SWITCH)]


class TeamSwitchOut(BaseModel):
    id: str
    edition_id: str
    team_id: str
    state: str  # previewed, queued, applying, done, stale or failed
    to_switch: list[str]  # Units that would be replaced (before the application)
    blocked: dict[str, list[str]]  # Unit -> closed words: needs_confirmation, adopted_original…
    pending: list[str]  # Units whose preferred team has fetched nothing yet
    switched: list[str]  # Units replaced (after the application)
    created_at: datetime


def build_teams_router(get_service: Callable[[], TeamSwitchService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/editions", tags=["teams"])

    def refusal(error: TeamSwitchRefusedError) -> ProblemError:
        return ProblemError(error.code)

    @router.post("/{edition_id}/team-switch", status_code=201)
    async def team_switch_preview(edition_id: str, principal: Switcher) -> TeamSwitchOut:
        try:
            view: dict[str, Any] = await run_in_threadpool(
                lambda: get_service().preview(principal, edition_id)
            )
        except TeamSwitchRefusedError as error:
            raise refusal(error) from None
        return TeamSwitchOut(**view)

    @router.post(
        "/{edition_id}/team-switch/{switch_id}/apply",
        status_code=202,
        responses={202: {"model": JobAccepted}},
    )
    def team_switch_apply(edition_id: str, switch_id: str, principal: Switcher) -> Any:
        try:
            with refusals():  # the queue is not running: a problem of the catalog, never a 500
                job_id = get_service().queue(principal, edition_id, switch_id)
        except TeamSwitchRefusedError as error:
            raise refusal(error) from None
        return accepted_job(job_id)

    @router.get("/{edition_id}/team-switch/{switch_id}")
    def team_switch_report(edition_id: str, switch_id: str, principal: Switcher) -> TeamSwitchOut:
        try:
            return TeamSwitchOut(**get_service().report(principal, edition_id, switch_id))
        except TeamSwitchRefusedError as error:
            raise refusal(error) from None

    return router
