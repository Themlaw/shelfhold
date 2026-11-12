"""`/api/v1/shelves`: create, bind, set the ceiling and the profiles of a shelf (EF-EXP-047).

Every change of what a shelf below 18+ may hold is a reserved action (admin session, fresh, never
a key): the ceiling (`shelf.max_age_rating`, which answers with the removal plan of what no longer
fits), the allowed profiles (`shelf.allowed_profiles`), choosing a family shelf for an Edition
(`shelf.choose_family`) and applying a removal plan (`shelf.withdraw`). The audit entry is written
in the transaction of the change.
"""

from collections.abc import Callable
from typing import Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.precreate import AdoptedLibrary
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless.actor import Actor
from shelfhold.identity.public import RESERVED_ACTIONS, Principal, audit_actor
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops.errors import OpsError

__all__ = ["build_edition_shelf_router", "build_shelves_router"]

_CEILING = RESERVED_ACTIONS["shelf.max_age_rating"]
_PROFILES = RESERVED_ACTIONS["shelf.allowed_profiles"]
_FAMILY = RESERVED_ACTIONS["shelf.choose_family"]
_WITHDRAW = RESERVED_ACTIONS["shelf.withdraw"]


def _actor(principal: Principal) -> Actor:
    return Actor(audit_actor(principal), principal.session_id, principal.auth_time)


class ShelfIn(BaseModel):
    name: str = Field(min_length=1)
    phrase: str = Field(min_length=1)
    max_age_rating: int | None = Field(default=None, ge=0, le=99)  # 99: X; the tiers: 422 below
    retention_mode: Literal["passthrough", "keep"] = "passthrough"
    hold_until_rated: bool | None = None  # on by default below 18+ (EF-EXP-050)
    allowed_profiles: list[str] = Field(default_factory=list)
    skip_read_upgrades: bool = True  # a read Unit is never replaced by a better one (EF-EXP-016)


class AdoptedLibraryIn(BaseModel):
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)


class PrecreateIn(BaseModel):
    adopted: list[AdoptedLibraryIn] = Field(default_factory=list)


class PrecreatedOut(BaseModel):
    created: list[str]


class SkipReadUpgradesIn(BaseModel):
    skip_read_upgrades: bool


class CeilingIn(BaseModel):
    max_age_rating: int | None = Field(default=None, ge=0, le=99)


class ProfilesIn(BaseModel):
    profiles: list[str]


class ProfilesOut(BaseModel):
    profiles: list[str]


class EditionShelfIn(BaseModel):
    shelf_id: str | None = None  # none: the shelf the kind of the Work proposes, by the same door


class FiledOut(BaseModel):
    filed: bool
    reason: str | None = None


class WithdrawIn(BaseModel):
    if_match: str
    shelf_id: str | None = None
    destination_id: str | None = None  # the unrated files of an adopted child library


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


def build_shelves_router(
    get_service: Callable[[], ShelfService], get_ops: Callable[[], Any]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/shelves", tags=["shelves"])

    @router.post("", status_code=201)
    def create_shelf(
        body: ShelfIn,
        principal: Principal = Depends(reserved_action(_CEILING)),  # noqa: B008
    ) -> ShelfOut:
        try:
            shelf_id = get_service().create_shelf(
                name=body.name, phrase=body.phrase, max_age_rating=body.max_age_rating,
                retention_mode=body.retention_mode, hold_until_rated=body.hold_until_rated,
                allowed_profiles=tuple(body.allowed_profiles), actor=_actor(principal),
                skip_read_upgrades=body.skip_read_upgrades,
            )  # fmt: skip
        except BindingError as error:
            raise ProblemError(error.code) from None
        return ShelfOut(id=shelf_id)

    @router.post("/precreate", status_code=201)
    def precreate(
        body: PrecreateIn,
        _principal: Principal = Depends(reserved_action(_CEILING)),  # noqa: B008
    ) -> PrecreatedOut:
        """The shelves a new instance starts with and one per adopted library (EF-UX-007);
        idempotent, a shelf that exists is never rewritten."""
        made = get_service().precreate([AdoptedLibrary(a.name, a.kind) for a in body.adopted])
        return PrecreatedOut(created=list(made.created))

    @router.post("/{shelf_id}/skip-read-upgrades")
    def set_skip_read_upgrades(
        shelf_id: str,
        body: SkipReadUpgradesIn,
        principal: Principal = Depends(reserved_action(_CEILING)),  # noqa: B008
    ) -> SkipReadUpgradesIn:
        """ "Ne pas mettre à niveau les Unités déjà lues" (EF-EXP-016), on by default: every path
        that asks for an upgrade reads it."""
        try:
            get_service().set_skip_read_upgrades(
                shelf_id, body.skip_read_upgrades, actor=_actor(principal)
            )
        except LookupError:
            raise ProblemError("not_found") from None
        return body

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

    @router.post("/{shelf_id}/max-age")
    def set_max_age(
        shelf_id: str,
        body: CeilingIn,
        principal: Principal = Depends(reserved_action(_CEILING)),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            change = get_service().set_max_age(
                shelf_id, body.max_age_rating, actor=_actor(principal)
            )
        except BindingError as error:
            raise ProblemError(error.code) from None
        except sa.exc.NoResultFound:
            raise ProblemError("not_found") from None
        return {
            "shelf_id": shelf_id,
            "previous": change.previous,
            "max_age_rating": change.new,
            "withdrawals": [
                {"edition_id": i.edition_id, "reason": i.reason} for i in change.withdrawals.items
            ],
            "withdrawals_version": change.withdrawals.version,
        }

    @router.post("/{shelf_id}/allowed-profiles")
    def set_profiles(
        shelf_id: str,
        body: ProfilesIn,
        principal: Principal = Depends(reserved_action(_PROFILES)),  # noqa: B008
    ) -> ProfilesOut:
        try:
            profiles = get_service().set_allowed_profiles(
                shelf_id, body.profiles, actor=_actor(principal)
            )
        except LookupError:
            raise ProblemError("not_found") from None
        return ProfilesOut(profiles=profiles)

    @router.get("/withdrawals")
    def withdrawal_plan(
        shelf_id: str | None = None,
        destination_id: str | None = None,
        _principal: Principal = Depends(reserved_action(_WITHDRAW)),  # noqa: B008
    ) -> dict[str, Any]:
        """What the admission refuses on the shelves below 18+ (or, with `destination_id`, the
        unrated files of an adopted child library); nothing moves."""
        try:
            plan: dict[str, Any] = get_ops().library.withdraw_plan(shelf_id, destination_id)
        except OpsError as error:
            raise ProblemError(error.code) from None
        return plan

    @router.post("/withdrawals")
    def withdrawal_apply(
        body: WithdrawIn,
        principal: Principal = Depends(reserved_action(_WITHDRAW)),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            done: dict[str, Any] = get_ops().library.withdraw_apply(
                if_match=body.if_match,
                shelf_id=body.shelf_id,
                destination_id=body.destination_id,
                principal=principal,
            )
        except OpsError as error:
            raise ProblemError(error.code) from None
        except StalePlanError:
            raise ProblemError("plan.stale") from None
        return done

    return router


def build_edition_shelf_router(get_service: Callable[[], ShelfService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/editions", tags=["shelves"])

    @router.get("/{edition_id}/shelf-proposal")
    def shelf_proposal(
        edition_id: str,
        _principal: Principal = Depends(reserved_action(_FAMILY)),  # noqa: B008
    ) -> dict[str, str | None]:
        """The shelf the kind of the Work proposes, unless a shelf below 18+ would refuse it."""
        return {"shelf_id": get_service().proposed_shelf(edition_id)}

    @router.post("/{edition_id}/shelf")
    def file_edition(
        edition_id: str,
        body: EditionShelfIn,
        principal: Principal = Depends(reserved_action(_FAMILY)),  # noqa: B008
    ) -> FiledOut:
        """File an Edition on a shelf; a shelf below 18+ takes it only through the admission."""
        service = get_service()
        if body.shelf_id is None:
            result = service.file_default(edition_id, actor=_actor(principal))
        else:
            result = service.file_edition(edition_id, body.shelf_id, actor=_actor(principal))
        return FiledOut(filed=result.filed, reason=result.reason)

    return router
