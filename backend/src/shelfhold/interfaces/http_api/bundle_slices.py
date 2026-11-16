"""`/api/v1/editions/{id}/bundle` and `bundle-slice`: the slices of the block of a series (M4 L49b,
ADR-0053 §1 and §8, EF-INT-007).

The admin's alone (403 for a child or a member, by session and by key; a device token opens the
feed and the blocks, nothing here). The read plans the parts from the manifest and the written
boundaries and writes nothing: no file is opened, no boundary written, no version made. The write
is a plain setting (the CSRF of a session applies): a whole number of chapters from 1 to 256, or
`null` to take the slice away. It moves no boundary already written, so a file an e-reader holds
keeps its part, and an Edition that does not exist answers 404.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import Engine

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.reading.bundle import BundleLimits, slices

__all__ = ["build_bundle_slices_router"]

Admin = Annotated[Principal, Depends(require_admin)]


class BundlePartOut(BaseModel):
    index: int
    chapters: int
    first_number: str
    last_number: str
    size: int | None  # the sizes of the files in the manifest; None when one is not known
    written: bool  # its start is written: a slice no longer moves it


class BundleExcludedOut(BaseModel):
    format: int  # a chapter that is not a CBZ
    unsafe: int  # a chapter whose path the block will not follow


class BundleCanOut(BaseModel):
    set_slice: bool


class BundleOut(BaseModel):
    edition_id: str
    slice_chapters: int | None
    slice_min: int
    slice_max: int
    parts: list[BundlePartOut]
    excluded: BundleExcludedOut
    can: BundleCanOut


class BundleSliceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slice_chapters: StrictInt | None = Field(default=None, ge=slices.SLICE_MIN, le=slices.SLICE_MAX)


def build_bundle_slices_router(
    get_engine: Callable[[], Engine], now: Callable[[], datetime]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["bundle"])
    limits = BundleLimits()

    def shown(engine: Engine, edition_id: str) -> BundleOut:
        with engine.connect() as connection:
            if not slices.edition_exists(connection, edition_id):
                raise ProblemError("not_found")
            described = slices.describe(connection, edition_id, limits)
        return BundleOut(
            edition_id=edition_id,
            slice_chapters=described.slice_chapters,
            slice_min=slices.SLICE_MIN,
            slice_max=slices.SLICE_MAX,
            parts=[
                BundlePartOut(
                    index=p.index, chapters=p.chapters, first_number=p.first_number,
                    last_number=p.last_number, size=p.size, written=p.written,
                )
                for p in described.parts
            ],
            excluded=BundleExcludedOut(**described.excluded),
            can=BundleCanOut(set_slice=True),
        )  # fmt: skip

    @router.get("/editions/{edition_id}/bundle")
    def bundle_slices_read(edition_id: str, _admin: Admin) -> BundleOut:
        return shown(get_engine(), edition_id)

    @router.post("/editions/{edition_id}/bundle-slice")
    def bundle_slices_set(edition_id: str, body: BundleSliceIn, _admin: Admin) -> BundleOut:
        engine = get_engine()
        shown(engine, edition_id)  # an Edition that is not there answers before anything is written
        slices.save(engine, edition_id, body.slice_chapters, now())
        return shown(engine, edition_id)

    return router
