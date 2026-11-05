"""`/api/v1/shelves`: create a shelf, bind it to a destination (EF-EXP-002, EF-EXP-058, EF-INT-024).

Setting a ceiling is the reserved action `shelf.max_age_rating` (admin session, fresh). In M1a the
ceiling must be 18+ (or X, or none): a lower one is refused, nothing is stored.
"""

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.domain.bindings import BindingError
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_shelves_router"]

_CEILING = RESERVED_ACTIONS["shelf.max_age_rating"]


class ShelfIn(BaseModel):
    name: str = Field(min_length=1)
    phrase: str = Field(min_length=1)
    max_age_rating: int | None = Field(default=None, ge=0, le=99)  # 99: X
    retention_mode: Literal["passthrough", "keep"] = "passthrough"


class ShelfOut(BaseModel):
    id: str


class DestinationIn(BaseModel):
    target_id: str
    transport: Literal["tree", "dropbox", "pull"] = "tree"
    plugin_egress: Literal["foyer", "externe"] = "foyer"
    attested_egress: Literal["foyer", "externe"] | None = None
    root_subpath: str = ""


class DestinationOut(BaseModel):
    id: str


def build_shelves_router(get_service: Callable[[], ShelfService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/shelves", tags=["shelves"])

    @router.post("", status_code=201)
    def create_shelf(
        body: ShelfIn,
        _principal: Principal = Depends(reserved_action(_CEILING)),  # noqa: B008
    ) -> ShelfOut:
        try:
            shelf_id = get_service().create_shelf(
                name=body.name, phrase=body.phrase, max_age_rating=body.max_age_rating,
                retention_mode=body.retention_mode,
            )  # fmt: skip
        except BindingError as error:
            raise ProblemError(error.code) from None
        return ShelfOut(id=shelf_id)

    @router.post("/{shelf_id}/destinations", status_code=201)
    def bind_destination(
        shelf_id: str,
        body: DestinationIn,
        _principal: Principal = Depends(reserved_action(_CEILING)),  # noqa: B008
    ) -> DestinationOut:
        try:
            destination_id = get_service().bind_destination(
                shelf_id=shelf_id, target_id=body.target_id, transport=body.transport,
                plugin_egress=body.plugin_egress, attested_egress=body.attested_egress,
                root_subpath=body.root_subpath,
            )  # fmt: skip
        except BindingError as error:
            raise ProblemError(error.code) from None
        return DestinationOut(id=destination_id)

    return router
