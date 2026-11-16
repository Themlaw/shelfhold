"""`/api/v1/shelves`: create, bind, set the ceiling and the profiles of a shelf (EF-EXP-047).

Every change of what a shelf below 18+ may hold is a reserved action (admin session, fresh, never
a key): the binding of a destination (`shelf.bind_destination`, EF-EXP-002), the ceiling
(`shelf.max_age_rating`, which answers with the removal plan of what no longer
fits), the allowed profiles (`shelf.allowed_profiles`), choosing a family shelf for an Edition
(`shelf.choose_family`) and applying a removal plan (`shelf.withdraw`). The audit entry is written
in the transaction of the change.

"Ajouter directement" (`POST /editions/{id}/add-directly`, M3 L09, EF-BIB-055) is an adult's own
session (admin or member, never a child, a key or a token): an Edition nobody rated never goes to
a family shelf, and an adult without the right to classify does not choose one.
"""

import contextlib
from collections.abc import Callable
from typing import Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.precreate import AdoptedLibrary
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless.actor import Actor
from shelfhold.identity.public import RESERVED_ACTIONS, Principal, audit_actor
from shelfhold.interfaces.http_api.auth import require_session, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops.errors import OpsError

__all__ = ["build_edition_shelf_router", "build_shelves_router"]

_CEILING = RESERVED_ACTIONS["shelf.max_age_rating"]
_PROFILES = RESERVED_ACTIONS["shelf.allowed_profiles"]
_FAMILY = RESERVED_ACTIONS["shelf.choose_family"]
_WITHDRAW = RESERVED_ACTIONS["shelf.withdraw"]
_BIND = RESERVED_ACTIONS["shelf.bind_destination"]


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


class AddDirectlyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shelf_id: str = Field(min_length=1, max_length=64)  # a shelf is always named: it is a choice


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
    # a folder that already is a tree is taken on purpose (ADR-0052): the server indexes it
    tree_id: str | None = Field(default=None, min_length=26, max_length=26)


class DestinationOut(BaseModel):
    id: str


class BindingOut(BaseModel):
    id: str
    target_id: str
    transport: str
    tree_id: str | None
    role: Literal["primary", "secondary"] | None
    partitioning: Literal["none", "library", "user"]
    present_units: list[str] | None = None  # with `edition_id`: what this binding holds of it


class BindingsOut(BaseModel):
    destinations: list[BindingOut]


class TargetOut(BaseModel):
    id: str
    name: str
    kind: str
    partitioning: Literal["none", "library", "user"]


class TargetsOut(BaseModel):
    items: list[TargetOut]


def _admin_session(request: Request) -> Principal:
    principal = require_session(request)
    if principal.role != "admin":
        raise ProblemError("forbidden")
    return principal


def build_targets_router(get_service: Callable[[], ShelfService]) -> APIRouter:
    """`GET /api/v1/targets`: the servers the shelves screen offers for a new binding (EF-EXP-002).
    An admin session: a key, a member and a child are refused. Nothing of the config."""
    router = APIRouter(prefix="/api/v1/targets", tags=["targets"])

    @router.get("")
    def list_targets(_principal: Principal = Depends(_admin_session)) -> TargetsOut:  # noqa: B008
        return TargetsOut(
            items=[
                TargetOut(
                    id=t.id,
                    name=t.name,
                    kind=t.kind,
                    partitioning=t.partitioning,  # type: ignore[arg-type]
                )
                for t in get_service().list_targets()
            ]
        )

    return router


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
        principal: Principal = Depends(reserved_action(_BIND)),  # noqa: B008
    ) -> DestinationOut:
        """A shelf takes one binding per folder and server (fan-out): each is looked at for the
        barrier of a shelf below 18+, and a folder that is a tree is attached to on purpose."""
        with contextlib.suppress(LookupError):  # an unknown shelf is answered by the bind below
            # a write route: a binding made before trees existed gets its tree first, so that it
            # can be attached to (the read route above never writes)
            get_service().list_bindings(shelf_id)
        try:
            destination_id = get_service().bind_destination(
                shelf_id=shelf_id, target_id=body.target_id, transport=body.transport,
                plugin_egress=body.plugin_egress, attested_egress=body.attested_egress,
                root_subpath=body.root_subpath, attach_to_tree=body.tree_id,
                actor=_actor(principal),
            )  # fmt: skip
        except BindingError as error:
            raise ProblemError(error.code) from None
        except sa.exc.NoResultFound:
            raise ProblemError("not_found") from None
        return DestinationOut(id=destination_id)

    @router.get("/{shelf_id}/destinations")
    def list_destinations(
        shelf_id: str,
        edition_id: str | None = None,
        _principal: Principal = Depends(reserved_action(_BIND)),  # noqa: B008
    ) -> BindingsOut:
        """The bindings of a shelf, with their tree and role; with `edition_id`, the Units of that
        Edition each binding holds (`present_in`, per binding: a tree is one copy, its indexers
        say what its primary says)."""
        try:
            views = get_service().read_bindings(shelf_id)
        except LookupError:
            raise ProblemError("not_found") from None
        held = get_ops().library.present_units(edition_id, [v.destination_id for v in views])
        return BindingsOut(
            destinations=[
                BindingOut(
                    id=v.destination_id,
                    target_id=v.target_id,
                    transport=v.transport,
                    tree_id=v.tree_id,
                    role=v.role,  # type: ignore[arg-type]
                    partitioning=v.partitioning,  # type: ignore[arg-type]
                    present_units=None if held is None else sorted(held[v.destination_id]),
                )
                for v in views
            ]
        )

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


def _adult_session(principal: Principal = Depends(require_session)) -> Principal:  # noqa: B008
    """An adult's own session: a child is refused here, a key or a token before that."""
    if principal.role not in ("admin", "member"):
        raise ProblemError("forbidden")
    return principal


def build_edition_shelf_router(get_service: Callable[[], ShelfService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/editions", tags=["shelves"])

    @router.get("/{edition_id}/shelf-proposal")
    def shelf_proposal(
        edition_id: str,
        _principal: Principal = Depends(reserved_action(_FAMILY)),  # noqa: B008
    ) -> dict[str, str | None]:
        """The shelf the kind of the Work proposes, unless a shelf below 18+ would refuse it."""
        return {"shelf_id": get_service().proposed_shelf(edition_id)}

    @router.post("/{edition_id}/add-directly")
    def add_directly(
        edition_id: str,
        body: AddDirectlyIn,
        request: Request,
        principal: Principal = Depends(_adult_session),  # noqa: B008
    ) -> FiledOut:
        """ "Ajouter directement": the row of the truth table, then the admission of every road.
        Refused (`filed: false`, with the reason) is an answer, not an error: the Edition waits in
        "À classer" and the adult may ask instead."""
        try:
            if get_service().touches_family(edition_id, body.shelf_id):
                # not a second door: a family shelf, in or out, is `shelf.choose_family`'s, with its
                # roles and its freshness; the guard answers 403 or 401 as it does on `/shelf`
                principal = reserved_action(_FAMILY)(request)
            result = get_service().add_directly(
                edition_id, body.shelf_id, role=principal.role, can_rate=principal.can_rate,
                actor=_actor(principal),
            )  # fmt: skip
        except (LookupError, sa.exc.NoResultFound, sa.exc.IntegrityError):
            raise ProblemError("not_found") from None
        return FiledOut(filed=result.filed, reason=result.reason)

    @router.post("/{edition_id}/shelf")
    def file_edition(
        edition_id: str,
        body: EditionShelfIn,
        principal: Principal = Depends(reserved_action(_FAMILY)),  # noqa: B008
    ) -> FiledOut:
        """File an Edition on a shelf; a shelf below 18+ takes it only through the admission."""
        service = get_service()
        try:
            if body.shelf_id is None:
                result = service.file_default(edition_id, actor=_actor(principal))
            else:
                result = service.file_edition(edition_id, body.shelf_id, actor=_actor(principal))
        except (LookupError, sa.exc.NoResultFound, sa.exc.IntegrityError):
            raise ProblemError("not_found") from None
        return FiledOut(filed=result.filed, reason=result.reason)

    return router
