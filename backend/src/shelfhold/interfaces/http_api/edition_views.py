"""What the series sheet and the profiles register read, and nothing they write.

`GET /api/v1/editions/{id}/units`: the Units of an Edition in reading order, `limit`/`offset`
pages with their `total`, and the read state of the profile that asks. Any profile, by session or
by key; an Edition it may not see (the one filter of every surface) answers exactly like an
absent one, a 404.

`GET /api/v1/editions/{id}/exports`: what was exported of an Edition, by destination, and the
blocks made for the e-readers. The admin's (403 for anyone else, by session and by key).

`GET /api/v1/profiles/overview`: how many Works each mark of the age rule shows (`ceiling`,
repeated; the marks of the screen by default), and per profile how many it sees, its last reading
(when its visibility shows it) and its e-readers. The admin's.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import Engine

from shelfhold.identity.domain.roles import MAX_AGE_LIMIT
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops.edition_exports import edition_exports
from shelfhold.ops.profile_overview import DEFAULT_CEILINGS, NotAChildError, profiles_overview
from shelfhold.reading.application.edition_units import (
    MAX_UNITS,
    UnknownEditionError,
    edition_units,
)

__all__ = ["build_edition_views_router"]

Admin = Annotated[Principal, Depends(require_admin)]
Reader = Annotated[Principal, Depends(require_principal)]
MAX_CEILINGS = 16
_ABSENT = {"Cache-Control": "no-store"}


class EditionUnitOut(BaseModel):
    id: str
    number: str  # as the source wrote it; empty for an extra with no number
    title: str | None
    kind: str  # chapter, episode, book
    track: str  # main, annual, special, extra, oneshot, unnumbered
    volume_id: str | None
    added_at: datetime
    read_state: Literal["unread", "in_progress", "read"]  # the asking profile's own
    readable: bool  # a file the integrated reader opens (`/lire/unit/{id}`)


class EditionUnitsOut(BaseModel):
    edition_id: str
    items: list[EditionUnitOut]
    total: int
    limit: int
    offset: int


class ExportItemsOut(BaseModel):
    unit: int
    volume: int
    bundle: int
    adopted: int
    opds_bundle: int


class ExportDestinationOut(BaseModel):
    destination_id: str
    target_id: str
    target_name: str
    target_kind: str
    shelf_name: str
    enabled: bool
    exported: int  # records that stand for a file there
    pending: int  # planned, not written yet
    failed: int  # in error, or missing on the destination
    last_exported_at: datetime | None


class ExportBlockOut(BaseModel):
    profile_id: str
    username: str
    part_index: int
    number: int
    size: int
    created_at: datetime
    last_used_at: datetime | None


class EditionExportsOut(BaseModel):
    edition_id: str
    items: ExportItemsOut  # exported objects by kind
    destinations: list[ExportDestinationOut]
    kepub_items: int  # what OPDS and `/lite` offer a Kobo as KEPUB
    blocks: list[ExportBlockOut]  # the blocks of the series made for an e-reader (OPDS)


class AgeStepOut(BaseModel):
    ceiling: int
    works: int


class LastReadOut(BaseModel):
    at: datetime
    work_id: str
    edition_id: str
    title: str


class ProfileDeviceOut(BaseModel):
    id: str
    label: str | None
    kind: str
    last_seen_at: datetime | None


class ProfileOverviewOut(BaseModel):
    profile_id: str
    username: str
    role: str
    max_age_rating: int | None
    works: int  # the Works this profile sees now
    last_read: LastReadOut | None  # None: nothing read, or a private adult's
    devices: list[ProfileDeviceOut]  # the e-readers not retired


class ProfilesOverviewOut(BaseModel):
    total_works: int  # what an adult sees: the « no ceiling » mark
    steps: list[AgeStepOut]
    profiles: list[ProfileOverviewOut]


def build_edition_views_router(get_engine: Callable[[], Engine]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["catalog"])

    @router.get(
        "/editions/{edition_id}/units",
        operation_id="edition_units",
        response_model=EditionUnitsOut,
        responses={404: {"description": "not-found"}},
    )
    def units(
        edition_id: str,
        response: Response,
        principal: Reader,
        limit: Annotated[int, Query(ge=1, le=MAX_UNITS)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> EditionUnitsOut:
        response.headers["Cache-Control"] = "no-store"  # the read state is the profile's
        try:
            with get_engine().connect() as connection:
                page = edition_units(
                    connection, user_id=principal.user_id, edition_id=edition_id,
                    limit=limit, offset=offset,
                )  # fmt: skip
        except UnknownEditionError:
            raise ProblemError("not_found", headers=_ABSENT) from None
        return EditionUnitsOut(
            edition_id=page.edition_id,
            items=[
                EditionUnitOut(
                    id=u.id, number=u.number, title=u.title, kind=u.kind, track=u.track,
                    volume_id=u.volume_id, added_at=u.added_at, read_state=u.read_state,
                    readable=u.readable,
                )
                for u in page.items
            ],
            total=page.total, limit=limit, offset=offset,
        )  # fmt: skip

    @router.get(
        "/editions/{edition_id}/exports",
        operation_id="edition_exports",
        response_model=EditionExportsOut,
        responses={404: {"description": "not-found"}},
    )
    def exports(edition_id: str, response: Response, _admin: Admin) -> EditionExportsOut:
        response.headers["Cache-Control"] = "no-store"
        with get_engine().connect() as connection:
            found = edition_exports(connection, edition_id)
        if found is None:
            raise ProblemError("not_found", headers=_ABSENT)
        return EditionExportsOut(
            edition_id=found.edition_id,
            items=ExportItemsOut(**found.items),
            destinations=[ExportDestinationOut(**vars(d)) for d in found.destinations],
            kepub_items=found.kepub_items,
            blocks=[ExportBlockOut(**vars(b)) for b in found.blocks],
        )

    @router.get(
        "/profiles/overview", operation_id="profiles_overview", response_model=ProfilesOverviewOut
    )
    def overview(
        response: Response,
        admin: Admin,
        ceiling: Annotated[list[int] | None, Query()] = None,
        profile_id: Annotated[str | None, Query(max_length=26)] = None,
    ) -> ProfilesOverviewOut:
        response.headers["Cache-Control"] = "no-store"
        wanted = ceiling or list(DEFAULT_CEILINGS)
        if len(wanted) > MAX_CEILINGS or any(not 0 <= c <= MAX_AGE_LIMIT for c in wanted):
            raise ProblemError("validation_failed")
        wanted = sorted(set(wanted))  # the marks of a rule: in order, each once
        try:
            with get_engine().connect() as connection:
                found = profiles_overview(
                    connection, viewer_id=admin.user_id, ceilings=wanted, profile_id=profile_id
                )
        except NotAChildError:
            raise ProblemError("validation_failed") from None
        return ProfilesOverviewOut(
            total_works=found.total_works,
            steps=[AgeStepOut(ceiling=c, works=n) for c, n in found.steps],
            profiles=[
                ProfileOverviewOut(
                    profile_id=p.profile_id, username=p.username, role=p.role,
                    max_age_rating=p.max_age_rating, works=p.works,
                    last_read=None if p.last_read is None else LastReadOut(**vars(p.last_read)),
                    devices=[ProfileDeviceOut(**vars(d)) for d in p.devices],
                )
                for p in found.profiles
            ],
        )  # fmt: skip

    return router
