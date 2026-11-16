"""`/api/v1/ops/...`: the activity history, the blocklist and the storage and network dashboard
(EF-INT-019, EF-INT-020).

All of it is the administrator's: a session of an administrator, or a key of scope `admin`. A
child, a member, a read-only key (the administrator's own included), a device token and a request
key are refused (403) before anything is read, and a block that exists is refused like one that
does not (no IDOR to tell them apart). Nothing in an answer is a path, a host, an address or a
secret: the history keeps a closed list of fields, the dashboard has sizes and counts, a block is
shown by its chapter's title and number.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Engine

from shelfhold.acquisition.application.blocklist import list_blocks
from shelfhold.identity.public import Credential, Principal
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops.activity_history import (
    BlockView,
    activity_history,
    describe_blocks,
    record_block,
    remote_id_of,
    remove_block,
)
from shelfhold.ops.storage_dashboard import MAX_DAYS, storage_dashboard

__all__ = ["build_ops_dashboard_router", "require_admin_surface"]


def require_admin_surface(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    """The administrator's session, or her key of scope `admin`. A read-only key is not enough, and
    nor is any other credential."""
    if principal.role != "admin":
        raise ProblemError("forbidden")
    if principal.credential is Credential.SESSION:
        return principal
    if (
        principal.credential is Credential.API_KEY
        and "admin" in principal.scopes
        and not principal.read_only
    ):
        return principal
    raise ProblemError("forbidden")


Admin = Annotated[Principal, Depends(require_admin_surface)]


class HistoryEntryOut(BaseModel):
    event_id: int
    type: str
    visibility: str
    occurred_at: datetime
    title: str | None
    title_masked: bool
    payload: dict[str, bool | int | str]


class HistoryOut(BaseModel):
    items: list[HistoryEntryOut]
    next_before: int | None  # pass it as `before` for the older ones; None: that was the oldest


class BlockIn(BaseModel):
    source_key: str = Field(min_length=1, max_length=200)
    remote_unit_id: str | None = Field(default=None, min_length=1, max_length=2000)
    unit_id: str | None = Field(default=None, min_length=26, max_length=26)  # or: our own Unit
    reason: Literal["bad_quality", "wrong_content", "duplicate", "other"]

    @model_validator(mode="after")
    def _one_way_to_name_the_chapter(self) -> "BlockIn":
        if (self.remote_unit_id is None) == (self.unit_id is None):
            raise ValueError("name the chapter by `remote_unit_id` or by `unit_id`, not both")
        return self


class BlockOut(BaseModel):
    id: str
    source_key: str
    ref: str  # a short digest of what the source calls the chapter, never the identifier itself
    title: str | None  # the Work and the number, when the chapter is known here
    number: str | None
    reason: str
    created_at: datetime
    proposed_count: int  # how many times the source offered it again and it was ignored
    last_proposed_at: datetime | None


class BlocksOut(BaseModel):
    items: list[BlockOut]


class ShelfOut(BaseModel):
    shelf_id: str | None  # None: what is filed on no shelf
    name: str | None
    bytes: int
    copies: int
    by_location: dict[str, int]


class TrafficDayOut(BaseModel):
    day: date
    requests: int
    bytes_in: int


class SourceTrafficOut(BaseModel):
    source_key: str
    requests: int
    bytes_in: int
    by_day: list[TrafficDayOut]


class TrafficOut(BaseModel):
    days: int
    sources: list[SourceTrafficOut]


class StorageOut(BaseModel):
    shelves: list[ShelfOut]
    traffic: TrafficOut


def _block_out(view: BlockView) -> BlockOut:
    b = view.block
    return BlockOut(
        id=b.id, source_key=b.source_key, ref=view.ref, title=view.title, number=view.number,
        reason=b.reason, created_at=b.created_at, proposed_count=b.proposed_count,
        last_proposed_at=b.last_proposed_at,
    )  # fmt: skip


def build_ops_dashboard_router(
    get_engine: Callable[[], Engine], now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ops", tags=["operations"])

    @router.get("/activity-history", response_model=HistoryOut)
    def ops_activity_history(
        admin: Admin,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: Annotated[int | None, Query(ge=1)] = None,
    ) -> HistoryOut:
        page = activity_history(get_engine(), admin, limit=limit, before=before)
        return HistoryOut(
            items=[HistoryEntryOut(**vars(entry)) for entry in page.items],
            next_before=page.next_before,
        )

    @router.get("/blocklist", response_model=BlocksOut)
    def ops_blocklist(_admin: Admin) -> BlocksOut:
        engine = get_engine()
        with engine.connect() as connection:
            blocks = list_blocks(connection)
        return BlocksOut(items=[_block_out(v) for v in describe_blocks(engine, blocks)])

    @router.post("/blocklist", response_model=BlockOut, status_code=201)
    def ops_block_chapter(admin: Admin, body: BlockIn, response: Response) -> BlockOut:
        engine = get_engine()
        remote = body.remote_unit_id
        if remote is None and body.unit_id is not None:  # (the model makes it one or the other)
            remote = remote_id_of(engine, body.unit_id, body.source_key)
        if remote is None:  # that source never offered that Unit: nothing to block
            raise ProblemError("not_found")
        blocked, created = record_block(
            engine, admin, source_key=body.source_key, remote_unit_id=remote,
            reason=body.reason, now=now(),
        )  # fmt: skip
        if not created:
            response.status_code = 200  # the same chapter, the same block
        return _block_out(describe_blocks(engine, [blocked])[0])

    @router.delete("/blocklist/{block_id}", status_code=204)
    def ops_unblock_chapter(admin: Admin, block_id: str) -> Response:
        """The one way a block ends, named in the audit. Nothing else lifts it."""
        if remove_block(get_engine(), admin, block_id, now=now()) is None:
            raise ProblemError("not_found")
        return Response(status_code=204)

    @router.get("/storage-dashboard", response_model=StorageOut)
    def ops_storage_dashboard(_admin: Admin, days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 30) -> StorageOut:
        board = storage_dashboard(get_engine(), today=now().date(), days=days)
        return StorageOut(
            shelves=[ShelfOut(**vars(s)) for s in board.shelves],
            traffic=TrafficOut(
                days=board.days,
                sources=[
                    SourceTrafficOut(
                        source_key=t.source_key,
                        requests=t.requests,
                        bytes_in=t.bytes_in,
                        by_day=[TrafficDayOut(**vars(d)) for d in t.by_day],
                    )
                    for t in board.sources
                ],
            ),
        )

    return router
