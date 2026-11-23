"""`/api/v1/profiles/{user_id}/trackers` (M6 L13, EF-INT-010): the tracker plugins a profile
links to their own remote reading account.

The link is the profile's own — nobody links another's, the admin included; a child links one
only where the admin allowed it (`tracker.allow_child`, a reserved action the admin's fresh
session performs on the `/allowance` route). Linking hands a token over, so it is a reserved
action too (`tracker.link`: fresh in every profile, never a key's), as is saying yes to the push
(`tracker.push_opt_in`) and removing the link (`tracker.unlink`). The token is in the request
body once and nowhere else: the answers carry its fingerprint. A pull brings back a
`TrackerHint` and writes nothing: no answer of these routes ever marks anything read.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from shelfhold_sdk.tracker import TrackProgress

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.integrations.trackers import TrackerError, TrackerHost
from shelfhold.interfaces.http_api.auth import require_session, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_trackers_router"]

_ANY_ROLE = ("admin", "member", "child")  # the service refuses a child without the allowance
_LINK = reserved_action(RESERVED_ACTIONS["tracker.link"], roles=_ANY_ROLE, audit=False)
_UNLINK = reserved_action(RESERVED_ACTIONS["tracker.unlink"], roles=_ANY_ROLE, audit=False)
_OPT_IN = reserved_action(RESERVED_ACTIONS["tracker.push_opt_in"], roles=_ANY_ROLE, audit=False)
_ALLOW_CHILD = reserved_action(RESERVED_ACTIONS["tracker.allow_child"], audit=False)


class TrackerLinkIn(BaseModel):
    """The remote account's token, once. Nothing else: the plugin knows the rest."""

    model_config = ConfigDict(extra="forbid")
    token: StrictStr = Field(min_length=1, max_length=512)


class LinkOut(BaseModel):
    plugin_key: str
    linked_at: datetime
    push_enabled: bool
    token_fingerprint: str | None


class LinkItem(BaseModel):
    """One line of the listing: the fingerprint stays off it — the answers of the list carry
    no field whose name even says "token" (EF-INT-010)."""

    plugin_key: str
    linked_at: datetime
    push_enabled: bool


class LinkList(BaseModel):
    items: list[LinkItem]


class PushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class PushOut(BaseModel):
    push_enabled: bool


class ProgressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fraction: float = Field(ge=0.0, le=1.0)
    at: datetime | None = None


class PushedOut(BaseModel):
    pushed: bool


class HintOut(BaseModel):
    """A pull's answer: where the profile is on the remote service. `hint` is always true —
    this is a hint for the reader, never a read mark (EF-INT-010)."""

    fraction: float | None = None
    at: datetime | None = None
    hint: bool = True


class AllowanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed: bool


class AllowanceOut(BaseModel):
    allowed: bool


def _problem(error: TrackerError) -> ProblemError:
    code = error.code
    if code in ("not_found", "not_linked"):
        return ProblemError("tracker_not_found")
    if code == "child_refused":
        return ProblemError("tracker_child_refused")
    if code == "push_refused":
        return ProblemError("tracker_push_refused")
    if code == "invalid_token":
        return ProblemError("tracker_refused", extensions={"reason": code})
    return ProblemError("tracker_refused", extensions={"reason": code})


def build_trackers_router(get_host: Callable[[], TrackerHost]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/profiles", tags=["trackers"])

    def run[T](call: Callable[[], T]) -> T:
        try:
            return call()
        except TrackerError as error:
            raise _problem(error) from None

    @router.get("/{user_id}/trackers", response_model=LinkList)
    def list_links(user_id: str, principal: Annotated[Principal, Depends(require_session)]) -> Any:
        views = run(lambda: get_host().links(principal, user_id))
        return LinkList(
            items=[
                LinkItem(
                    plugin_key=v.plugin_key,
                    linked_at=v.linked_at,
                    push_enabled=v.push_enabled,
                )
                for v in views
            ]
        )

    @router.get("/{user_id}/trackers/{plugin_key}", response_model=LinkOut)
    def get_link(
        user_id: str,
        plugin_key: str,
        principal: Annotated[Principal, Depends(require_session)],
    ) -> Any:
        view = run(lambda: get_host().view(principal, user_id, plugin_key))
        return LinkOut(
            plugin_key=view.plugin_key,
            linked_at=view.linked_at,
            push_enabled=view.push_enabled,
            token_fingerprint=view.token_fingerprint,
        )

    @router.put("/{user_id}/trackers/{plugin_key}", response_model=LinkOut)
    def link(
        user_id: str,
        plugin_key: str,
        body: TrackerLinkIn,
        principal: Annotated[Principal, Depends(_LINK)],
    ) -> Any:
        view = run(lambda: get_host().link(principal, user_id, plugin_key, body.token))
        return LinkOut(
            plugin_key=view.plugin_key,
            linked_at=view.linked_at,
            push_enabled=view.push_enabled,
            token_fingerprint=view.token_fingerprint,
        )

    @router.delete("/{user_id}/trackers/{plugin_key}", status_code=204)
    def unlink(
        user_id: str,
        plugin_key: str,
        principal: Annotated[Principal, Depends(_UNLINK)],
    ) -> Response:
        run(lambda: get_host().unlink(principal, user_id, plugin_key))
        return Response(status_code=204)

    @router.put("/{user_id}/trackers/{plugin_key}/push", response_model=PushOut)
    def set_push(
        user_id: str,
        plugin_key: str,
        body: PushIn,
        principal: Annotated[Principal, Depends(_OPT_IN)],
    ) -> Any:
        enabled = run(lambda: get_host().set_push(principal, user_id, plugin_key, body.enabled))
        return PushOut(push_enabled=enabled)

    @router.post("/{user_id}/trackers/{plugin_key}/progress", response_model=PushedOut)
    def push(
        user_id: str,
        plugin_key: str,
        body: ProgressIn,
        principal: Annotated[Principal, Depends(require_session)],
    ) -> Any:
        progress = TrackProgress(fraction=body.fraction, at=body.at)
        run(lambda: get_host().push(principal, user_id, plugin_key, progress))
        return PushedOut(pushed=True)

    @router.post("/{user_id}/trackers/{plugin_key}/pull", response_model=HintOut)
    def pull(
        user_id: str,
        plugin_key: str,
        principal: Annotated[Principal, Depends(require_session)],
    ) -> Any:
        hint = run(lambda: get_host().pull(principal, user_id, plugin_key))
        if hint is None:
            return HintOut()
        return HintOut(fraction=hint.fraction, at=hint.at)

    @router.put("/{user_id}/trackers/{plugin_key}/allowance", response_model=AllowanceOut)
    def set_allowance(
        user_id: str,
        plugin_key: str,
        body: AllowanceIn,
        principal: Annotated[Principal, Depends(_ALLOW_CHILD)],
    ) -> Any:
        allowed = run(
            lambda: get_host().set_allowance(principal, user_id, plugin_key, body.allowed)
        )
        return AllowanceOut(allowed=allowed)

    return router
