"""`/api/v1/follows`: follow a series of the connected service from Shelfhold, or stop (EF-SRC-029).

`POST` approves the request and makes the one entry in the service's library (a replay, a double
click or a second worker does not make another). `DELETE` stops following: the series stays in the
service unless `remove_upstream` is true, which needs `confirm` (409 `follow.confirmation_required`
otherwise). While the service is paused nothing is sent (409 `follow.service_paused`). Admin only;
a session needs its CSRF token like every state-changing route. Not reserved actions.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices, user_of

__all__ = ["build_follows_router"]

Admin = Annotated[Principal, Depends(require_admin)]


class FollowIn(BaseModel):
    source_key: str
    remote_id: str


class UnfollowIn(FollowIn):
    remove_upstream: bool = False
    confirm: bool = False


class FollowOut(BaseModel):
    state: Literal["followed", "unfollowed", "in_progress"]
    sent: bool  # false: nothing was sent to the service (already followed, or on its way)


class CheckOut(BaseModel):
    queued: int  # follow jobs queued (or joined) for the services this series comes through


def build_follows_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["follows"])

    @router.post("/follows")
    def follow(body: FollowIn, admin: Admin) -> FollowOut:
        with refusals():
            answer = get_ops().follows.follow(
                body.source_key, body.remote_id, requester_user_id=user_of(admin)
            )
        return FollowOut(**answer)

    @router.delete("/follows")
    def unfollow(body: UnfollowIn, _admin: Admin) -> FollowOut:
        with refusals():
            answer = get_ops().follows.unfollow(
                body.source_key, body.remote_id,
                remove_upstream=body.remove_upstream, confirm=body.confirm,
            )  # fmt: skip
        return FollowOut(state=answer["state"], sent=answer["sent"])

    @router.post("/editions/{edition_id}/check")
    def follow_check(edition_id: str, _admin: Admin) -> CheckOut:
        """ "Check now" (EF-SRC-038): looked at at the next pass, in the news class."""
        with refusals():
            answer = get_ops().follows.check(edition_id)
        return CheckOut(**answer)

    return router
