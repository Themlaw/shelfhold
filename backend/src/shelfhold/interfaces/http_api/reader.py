"""The routes of the integrated reader: what it opens (M5 L06) and where it is (M5 L07).

`GET /api/reader/{carrier_type}/{carrier_id}/publication`: what the reader opens (M5 L06).

The manifest of a book for the profile and the session that ask (EF-LEC-014, EF-LEC-005, D-P1):
the metadata, the direction of reading (`ltr`, `rtl`, `webtoon`), the pages of an image carrier or
the reading order of an EPUB, and a link into `/content` for each entry, carrying a ticket bound to
the session. The credential is the session cookie: a key of the API and a device token get no
ticket (403). A carrier that is absent, hidden from the profile, of a type that does not exist or
not an identifier answers one 404 (no oracle); a Work with no file that may be read, an excerpt that
cannot be cut within its bound, and a full read of what is only a sample are typed 409s that say
what to do. Nothing is written: no position, no history, no state.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from shelfhold.identity.public import Principal
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.interfaces.http_api.auth import require_session
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.reading.application.content_access import (
    ExcerptUnavailableError,
    FullReadNotAllowedError,
    NoReadableCopyError,
)
from shelfhold.reading.application.positions import (
    ClientTsInFutureError,
    ClientTsOutOfRangeError,
    Conflict,
    Current,
    ExcerptWritesNoPositionError,
    InvalidTicketError,
    ReaderPositions,
    StaleConflictError,
    StoredPosition,
    UnknownConflictError,
)
from shelfhold.reading.application.progress import HISTORY_LIMIT
from shelfhold.reading.application.publication import Publication, PublicationService
from shelfhold.reading.domain.locator import InvalidLocatorV1Error
from shelfhold.reading.public import UnknownCarrierError

__all__ = ["build_reader_router"]

Session = Annotated[Principal, Depends(require_session)]
_ABSENT = {"Cache-Control": "no-store"}


class EntryOut(BaseModel):
    index: int
    href: str
    media_type: str


class PublicationOut(BaseModel):
    carrier_type: Literal["content", "export_item"]
    carrier_id: str
    scope: Literal["full", "excerpt"]
    kind: Literal["pages", "epub"]
    title: str
    number: str | None
    language: str | None
    reading_direction: Literal["ltr", "rtl", "webtoon"]
    expires_at: datetime
    pages: list[EntryOut]
    spine: list[EntryOut]
    package: str | None


def _out(found: Publication) -> PublicationOut:
    return PublicationOut(
        carrier_type=found.carrier_type,  # type: ignore[arg-type]
        carrier_id=found.carrier_id,
        scope=found.scope,
        kind=found.kind,
        title=found.title,
        number=found.number,
        language=found.language,
        reading_direction=found.reading_direction,
        expires_at=found.expires_at,
        pages=[EntryOut(index=e.index, href=e.href, media_type=e.media_type) for e in found.pages],
        spine=[EntryOut(index=e.index, href=e.href, media_type=e.media_type) for e in found.spine],
        package=found.package,
    )


class PositionIn(BaseModel):
    """What a reader says of where it is. The profile is the session's: no field names one."""

    model_config = ConfigDict(extra="forbid")

    carrier_type: Literal["content", "export_item"]
    carrier_id: str = Field(max_length=26)
    locator: dict[str, Any]  # a locator v1 (ADR-0055), checked by the domain
    client_ts: AwareDatetime
    # a stable label of the device inside the profile; without it two devices count as one and no
    # conflict is proposed between them (the web reader sends a browser identifier, L10)
    device_id: str | None = Field(default=None, min_length=1, max_length=64)
    scope: Literal["full", "excerpt"] = "full"
    ticket: str | None = Field(default=None, max_length=1024)


class StoredOut(BaseModel):
    id: str
    locator: dict[str, Any]
    client_ts: datetime
    server_ts: datetime
    device_id: str | None
    approximate: bool  # written under another filter, or on a content since replaced


class ConflictOut(BaseModel):
    id: str
    winner: StoredOut  # the latest: where the reader is
    loser: StoredOut  # the further one, on another device
    created_at: datetime


class CurrentOut(BaseModel):
    position: StoredOut | None
    conflict: ConflictOut | None


class HistoryOut(BaseModel):
    items: list[StoredOut]
    limit: int


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: Literal["winner", "loser"]
    device_id: str | None = Field(default=None, min_length=1, max_length=64)


def _stored(found: StoredPosition) -> StoredOut:
    return StoredOut(
        id=found.id, locator=found.locator, client_ts=found.client_ts,
        server_ts=found.server_ts, device_id=found.device_id, approximate=found.approximate,
    )  # fmt: skip


def _conflict(found: Conflict | None) -> ConflictOut | None:
    if found is None:
        return None
    return ConflictOut(
        id=found.id, winner=_stored(found.winner), loser=_stored(found.loser),
        created_at=found.created_at,
    )  # fmt: skip


def _current(found: Current) -> CurrentOut:
    return CurrentOut(
        position=None if found.position is None else _stored(found.position),
        conflict=_conflict(found.conflict),
    )


def _own_session(principal: Principal) -> None:
    if principal.session_id is None or principal.via_forward:
        raise ProblemError("credential_not_allowed")  # the reader acts under a real session


def build_reader_router(
    get_service: Callable[[], PublicationService],
    get_positions: Callable[[], ReaderPositions],
) -> APIRouter:
    router = APIRouter(prefix="/api/reader", tags=["reader"])

    @router.get(
        "/{carrier_type}/{carrier_id}/publication",
        operation_id="reader_publication",
        response_model=PublicationOut,
        responses={404: {"description": "not-found"}, 409: {"description": "not-readable"}},
    )
    def publication(
        carrier_type: str,
        carrier_id: str,
        response: Response,
        principal: Session,
        scope: Annotated[Literal["full", "excerpt"] | None, Query()] = None,
    ) -> PublicationOut:
        if principal.session_id is None or principal.via_forward:
            raise ProblemError("credential_not_allowed")  # a ticket is bound to a real session
        if not is_ulid(carrier_id):
            raise ProblemError("not_found", headers=_ABSENT)
        try:
            found = get_service().publication(
                user_id=principal.user_id,
                session_id=principal.session_id,
                carrier_type=carrier_type,
                carrier_id=carrier_id,
                scope=scope,
            )
        except UnknownCarrierError:
            raise ProblemError("not_found", headers=_ABSENT) from None
        except FullReadNotAllowedError:
            raise ProblemError("full_read_not_allowed") from None
        except ExcerptUnavailableError:  # before its parent: a typed refusal of its own
            raise ProblemError("excerpt_unavailable") from None
        except NoReadableCopyError:
            raise ProblemError("no_readable_copy") from None
        response.headers["Cache-Control"] = "no-store"  # it holds tickets: kept nowhere
        return _out(found)

    @router.put(
        "/positions",
        operation_id="reader_put_position",
        response_model=CurrentOut,
        responses={
            404: {"description": "not-found"},
            409: {"description": "excerpt-writes-no-position"},
        },
    )
    def put_position(body: PositionIn, response: Response, principal: Session) -> CurrentOut:
        """Record where the reader is (locator v1, `client_ts` bounded by the server's clock);
        answers where she is now and any divergence to propose. Never for an excerpt."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        if not is_ulid(body.carrier_id):
            raise ProblemError("not_found", headers=_ABSENT)
        try:
            found = get_positions().write(
                user_id=principal.user_id, carrier_type=body.carrier_type,
                carrier_id=body.carrier_id, locator=body.locator, client_ts=body.client_ts,
                device_id=body.device_id, scope=body.scope, ticket=body.ticket,
            )  # fmt: skip
        except InvalidLocatorV1Error:
            raise ProblemError("invalid_locator") from None
        except ClientTsInFutureError:
            raise ProblemError("client_ts_in_future") from None
        except ClientTsOutOfRangeError:
            raise ProblemError("client_ts_out_of_range") from None
        except InvalidTicketError:
            raise ProblemError("validation_failed") from None
        except UnknownCarrierError:
            raise ProblemError("not_found", headers=_ABSENT) from None
        except ExcerptWritesNoPositionError:
            raise ProblemError("excerpt_writes_no_position") from None
        except NoReadableCopyError:
            raise ProblemError("no_readable_copy") from None
        return _current(found)

    @router.get(
        "/{carrier_type}/{carrier_id}/position",
        operation_id="reader_position",
        response_model=CurrentOut,
        responses={404: {"description": "not-found"}},
    )
    def position(
        carrier_type: str, carrier_id: str, response: Response, principal: Session
    ) -> CurrentOut:
        """Where the reader is on this file (the latest position), and the proposal if open."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        if carrier_type not in {"content", "export_item"} or not is_ulid(carrier_id):
            raise ProblemError("not_found", headers=_ABSENT)
        try:
            return _current(
                get_positions().current(
                    user_id=principal.user_id, carrier_type=carrier_type, carrier_id=carrier_id
                )
            )
        except UnknownCarrierError:
            raise ProblemError("not_found", headers=_ABSENT) from None

    @router.get(
        "/{carrier_type}/{carrier_id}/positions",
        operation_id="reader_position_history",
        response_model=HistoryOut,
        responses={404: {"description": "not-found"}},
    )
    def history(
        carrier_type: str,
        carrier_id: str,
        response: Response,
        principal: Session,
        limit: Annotated[int, Query(ge=1, le=HISTORY_LIMIT)] = 100,
    ) -> HistoryOut:
        """The positions written on this file, the latest first, at most `HISTORY_LIMIT`."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        if carrier_type not in {"content", "export_item"} or not is_ulid(carrier_id):
            raise ProblemError("not_found", headers=_ABSENT)
        try:
            items = get_positions().history(
                user_id=principal.user_id, carrier_type=carrier_type, carrier_id=carrier_id,
                limit=limit,
            )  # fmt: skip
        except UnknownCarrierError:
            raise ProblemError("not_found", headers=_ABSENT) from None
        return HistoryOut(items=[_stored(i) for i in items], limit=limit)

    @router.post(
        "/conflicts/{conflict_id}/resolve",
        operation_id="reader_resolve_conflict",
        response_model=CurrentOut,
        responses={404: {"description": "not-found"}, 409: {"description": "conflict-stale"}},
    )
    def resolve(
        conflict_id: str, body: ResolveIn, response: Response, principal: Session
    ) -> CurrentOut:
        """Settle the proposal: keep the latest position (`winner`) or resume at the further one
        (`loser`). Another profile's proposal, a settled one and an absent one answer alike."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        if not is_ulid(conflict_id):
            raise ProblemError("not_found", headers=_ABSENT)
        try:
            return _current(
                get_positions().resolve(
                    user_id=principal.user_id,
                    conflict_id=conflict_id,
                    choice=body.choice,
                    device_id=body.device_id,
                )
            )
        except (UnknownConflictError, UnknownCarrierError):
            raise ProblemError("not_found", headers=_ABSENT) from None
        except StaleConflictError:
            raise ProblemError("conflict_stale") from None

    return router
