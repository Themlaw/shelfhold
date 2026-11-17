"""`/api/v1/me/notification-destination` (M4 L39, EF-INT-021): the Apprise destination of one
adult profile, where the titles of their notices go (and nowhere else).

It is always the caller's own: no identifier in the path or the body, so nobody sets another's. A
session of an admin or a member, never a child (a child has no destination of her own), never an
API key or a token. Setting one is a reserved action (`notify.profile_destination`: handing over
a secret, so fresh in every profile). The URL is in the request body once; the answers say only
whether one is set.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.integrations.notify import ProfileDestinationError, ProfileDestinations
from shelfhold.interfaces.http_api.auth import require_session, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_notify_destination_router"]

_ADULTS = ("admin", "member")
_SET = reserved_action(RESERVED_ACTIONS["notify.profile_destination"], roles=_ADULTS)


class NotifyDestinationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: StrictStr = Field(min_length=1, max_length=2048)


class NotifyDestinationOut(BaseModel):
    configured: bool


def _adult(principal: Annotated[Principal, Depends(require_session)]) -> Principal:
    if principal.role not in _ADULTS:
        raise ProblemError("forbidden")
    return principal


def build_notify_destination_router(
    get_destinations: Callable[[], ProfileDestinations],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/me", tags=["notifications"])

    @router.get("/notification-destination", response_model=NotifyDestinationOut)
    def read(principal: Annotated[Principal, Depends(_adult)]) -> NotifyDestinationOut:
        return NotifyDestinationOut(configured=get_destinations().configured(principal.user_id))

    @router.put("/notification-destination", response_model=NotifyDestinationOut)
    def put(
        body: NotifyDestinationIn, principal: Annotated[Principal, Depends(_SET)]
    ) -> NotifyDestinationOut:
        try:
            get_destinations().set(principal.user_id, body.url)
        except ProfileDestinationError as error:
            if error.code == "invalid_url":
                raise ProblemError("notify_destination_refused") from None
            raise ProblemError("forbidden") from None
        return NotifyDestinationOut(configured=True)

    @router.delete("/notification-destination", status_code=204)
    def clear(principal: Annotated[Principal, Depends(_adult)]) -> Response:
        get_destinations().clear(principal.user_id)
        return Response(status_code=204)

    return router
