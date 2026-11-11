"""The routes behind the "À classer" screen and the "À ranger" view (EF-USR-015, EF-EXP-046).

Two lists, read by an admin session, and "classer tout en lot", the reserved action `rate`: a
fresh admin session, never a key, with the token the list gave back typed again. The batch calls
the same `RatingOps` as `shelfhold rate --pending`, one audit entry per Work. A raised age answers
with the plan of the files that no longer fit; nothing is withdrawn here (`shelf withdraw`).
The titles come from the sources: they are data for the client, which never reads them as markup.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import EditionSuggestionOut, RatedOut, refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["build_triage_router"]

_RATE = reserved_action(RESERVED_ACTIONS["rate"])


class ToRateItem(BaseModel):
    work_id: str
    title: str
    state: str
    reason: str
    suggestions: list[EditionSuggestionOut] = []  # open ones above an Edition's age


class ToRateOut(BaseModel):
    age: str | None
    items: list[ToRateItem]
    token: str | None  # only when an age is chosen: what the batch asks to be typed again


class ToShelveItem(BaseModel):
    edition_id: str
    work_id: str
    title: str
    lang: str
    media: str


class ToShelveOut(BaseModel):
    items: list[ToShelveItem]


class RateBatchIn(BaseModel):
    age: str = Field(min_length=1, max_length=8, examples=["13+", "18+", "X"])
    confirm: str = Field(min_length=1, max_length=64)


class BatchRatedOut(RatedOut):
    work_id: str


class RateBatchOut(BaseModel):
    age: str
    rated: list[BatchRatedOut]


def build_triage_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["triage"])

    @router.get("/to-rate", response_model=ToRateOut)
    def to_rate_list(
        _admin: Annotated[Principal, Depends(require_admin)], age: str | None = None
    ) -> ToRateOut:
        """The Works that wait in "À classer"; with `age`, the token that rates them all."""
        with refusals():
            return ToRateOut(**get_ops().rating.listing(age))

    @router.get("/to-shelve", response_model=ToShelveOut)
    def to_shelve_list(_admin: Annotated[Principal, Depends(require_admin)]) -> ToShelveOut:
        """The Editions on no shelf: not exported, kept as received (EF-EXP-046)."""
        with refusals():
            return ToShelveOut(items=[ToShelveItem(**i) for i in get_ops().rating.unshelved()])

    @router.post("/works/rating-batch", response_model=RateBatchOut)
    def rate_batch(
        body: RateBatchIn, principal: Annotated[Principal, Depends(_RATE)]
    ) -> RateBatchOut:
        """ "Classer tout en lot": reserved (EF-BIB-036); all of them or none."""
        with refusals():
            done = get_ops().rating.rate_batch(body.age, confirm=body.confirm, principal=principal)
        return RateBatchOut(**done)

    return router
