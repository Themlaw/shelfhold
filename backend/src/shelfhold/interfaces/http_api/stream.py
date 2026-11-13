"""`GET /api/v1/events`: the typed SSE stream of a profile (EF-INT-002).

The route is transport only. What is sent, and when, is decided by `events.stream.Feed`; each row
goes through `project_event` with the profile of the **reader** (a session's own, a key's own
profile, a child's ceiling read at the request), at the replay and at the live send alike. The
credential is asked again as the stream goes on (a revoked session ends it). Left out of the
OpenAPI document: the child meta-test walks the real routes and holds its case
(`tests/integration/test_child_surfaces.py`).

`Last-Event-ID` carries the sealed cursor of the last frame (or heartbeat) the browser got; one
that cannot be trusted is answered by a 200 stream holding a single `resync_required` frame.
`X-Accel-Buffering: no` keeps a reverse proxy from holding the frames back.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection, Engine
from starlette.concurrency import run_in_threadpool

from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.events.projection import project_event
from shelfhold.events.since_key import SinceKey
from shelfhold.events.stream import Cursor, Feed
from shelfhold.identity.public import Principal, credential_is_live, reader_scope
from shelfhold.infrastructure.clock import Clock
from shelfhold.interfaces.http_api.activity import activity_event_of, viewer_of
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.lists import decode_since, encode_since
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_stream_router", "cursor_codec", "frame_of"]

HEADERS = {
    "Cache-Control": "no-cache,no-transform",
    "X-Accel-Buffering": "no",
}


async def _wait(wake: asyncio.Event, timeout: float) -> None:
    """Until something may be due or the heartbeat is. A test replaces it with a function that
    moves its clock by `timeout`: no real sleep."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(wake.wait(), timeout)


def frame_of(
    connection: Connection, viewer: Any, scope: Any, row: Any, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """What `viewer` may see of `row` (its payload at the current version), as the JSON of a
    frame: the activity page's projection, `project_event`, and nothing else. Neither the number
    of the row nor the actor is in it."""
    item = project_event(connection, viewer, scope, activity_event_of(connection, row, payload))
    if item is None:
        return None
    return {
        "type": item.type,
        "visibility": item.visibility,
        "payload": dict(item.payload),
        "occurred_at": row.occurred_at.isoformat(),
        "title": item.title,
        "title_masked": item.title_masked,
    }


def cursor_codec(key: Callable[[], bytes], user_id: str) -> dict[str, Any]:
    """The `seal` and `unseal` of a stream's ids: the `?since=` token of L11, bound to the
    profile it is issued to (its `kind` carries the profile). A token that does not open, for
    whatever reason, is `None`: the stream says `resync_required`, never an error."""
    kind = f"events:{user_id}"

    def seal(cursor: Cursor) -> str:
        return encode_since(
            cursor.seq, key=key(), kind=kind, epoch=cursor.epoch, marker=cursor.marker,
            scope=cursor.scope,
        )  # fmt: skip

    def unseal(token: str) -> Cursor | None:
        try:
            since = decode_since(token, key=key(), kind=kind)
        except ProblemError:
            return None
        return Cursor(since.seq, since.epoch, since.marker, since.scope)

    return {"seal": seal, "unseal": unseal}


def build_stream_router(
    get_engine: Callable[[], Engine],
    get_bus: Callable[[], EventBus],
    clock: Clock,
    key: SinceKey,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["events"])

    @router.get("/events", include_in_schema=False)
    async def events(
        request: Request,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> StreamingResponse:
        engine = get_engine()
        scope = reader_scope(engine, principal.user_id)
        viewer = viewer_of(principal, scope.kind)

        def project(connection: Connection, row: Any, payload: dict[str, Any]) -> Any:
            return frame_of(connection, viewer, scope, row, payload)

        feed = Feed(
            engine,
            scope=scope,
            **cursor_codec(key.value, principal.user_id),
            project=project,
            clock=clock,
            alive=lambda: credential_is_live(engine, principal, clock.now()),
            scope_now=lambda: reader_scope(engine, principal.user_id),
            last_event_id=request.headers.get("last-event-id"),
        )
        return StreamingResponse(
            _frames(feed, get_bus()), media_type="text/event-stream", headers=HEADERS
        )

    return router


async def _frames(feed: Feed, bus: EventBus) -> AsyncIterator[str]:
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Subscribed before the head is read: nothing published in between is missed (the rows are
    # read by position, the bus only says "look").
    def look(_event: PublishedEvent) -> None:
        loop.call_soon_threadsafe(wake.set)

    unsubscribe = bus.subscribe(look)
    try:
        for frame in await run_in_threadpool(feed.start):
            yield frame
        while not feed.closed:
            wake.clear()  # before the read: a row that lands during it wakes the next wait
            for frame in await run_in_threadpool(feed.poll):
                yield frame
            if not feed.closed and not feed.backlog:
                await _wait(wake, feed.seconds_to_heartbeat())
            await asyncio.sleep(0)  # let a disconnect cancel us
    finally:
        unsubscribe()
