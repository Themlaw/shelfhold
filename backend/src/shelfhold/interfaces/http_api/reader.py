"""The routes of the integrated reader: what it opens (M5 L06), where it is (M5 L07), how it
reads (M5 L13) and the read marks it undoes (M6 L18).

`GET /api/reader/{carrier_type}/{carrier_id}/publication`: what the reader opens (M5 L06).

The manifest of a book for the profile and the session that ask (EF-LEC-014, EF-LEC-005, D-P1):
the metadata, the direction of reading (`ltr`, `rtl`, `webtoon`), the pages of an image carrier or
the reading order of an EPUB, and a link into `/content` for each entry, carrying a ticket bound to
the session. The credential is the session cookie: a key of the API and a device token get no
ticket (403). A carrier that is absent, hidden from the profile, of a type that does not exist or
not an identifier answers one 404 (no oracle); a Work with no file that may be read, an excerpt that
cannot be cut within its bound, and a full read of what is only a sample are typed 409s that say
what to do. Nothing is written: no position, no history, no state.

`GET /read-marks`, `POST /read-marks/{unit_id}/undo` (M6 L18): the marks of the profile's reading
on the Units of an Edition, and the undo of the profile's own mark — the administrator's, for the
mark of a child profile, on a door of their own. The undo is a line of history, nothing is deleted;
a Unit the profile may not see, and a mark that is not there, answer alike.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

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
from shelfhold.reading.application.continue_reading import (
    DEFAULT_ENTRIES,
    MAX_ENTRIES,
    ContinueEntry,
    ContinueReading,
)
from shelfhold.reading.application.lease import ProgressBusyError
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
from shelfhold.reading.application.progress import HISTORY_LIMIT, ReadMarks
from shelfhold.reading.application.publication import Publication, PublicationService
from shelfhold.reading.application.settings import (
    DIRECTIONS,
    DeviceScopeError,
    Effective,
    InvalidSettingError,
    ReaderSettings,
    SettingsLimitError,
)
from shelfhold.reading.domain.locator import InvalidLocatorV1Error
from shelfhold.reading.public import UnknownCarrierError

__all__ = ["build_reader_router"]

Session = Annotated[Principal, Depends(require_session)]
_ABSENT = {"Cache-Control": "no-store"}
_BUSY = {"Cache-Control": "no-store", "Retry-After": "2"}  # a job holds the Work (M5 L22a)


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
    series_id: str  # the Œuvre: the anchor of a setting pinned to this series (L13)
    work_id: str  # the same Œuvre, as `?since=` names it: the key of an offline copy (L16b)
    edition_id: str  # the Édition: `removed_editions` of `?since=` names it, a copy of it goes
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
        series_id=found.series_id,
        work_id=found.series_id,
        edition_id=found.edition_id,
        expires_at=found.expires_at,
        pages=[EntryOut(index=e.index, href=e.href, media_type=e.media_type) for e in found.pages],
        spine=[EntryOut(index=e.index, href=e.href, media_type=e.media_type) for e in found.spine],
        package=found.package,
    )


class ContinueItemOut(BaseModel):
    work_id: str
    title: str
    unit_id: str
    number: str
    unit_title: str | None
    state: Literal["in_progress", "next"]  # the Unit being read, or the one after the last read
    # the kept copy here, or the kind of the destination the Unit is read on (« Ouvrir dans Komga »)
    target: str
    approximate: bool
    # `target = kept_copy`: the pair the reader route takes, `/lire/<carrier_type>/<carrier_id>`
    carrier_type: Literal["content"] | None
    carrier_id: str | None
    # `target` names a destination: the address this entry opens — its own server's, the one the
    # profile's account is on (two servers of one kind are two addresses), else None
    open_url: str | None


class ContinueOut(BaseModel):
    items: list[ContinueItemOut]
    # every server the profile can open something on: the first of a kind under the kind's key,
    # a second server of the same kind under its target's id — each entry carries its own address
    open_urls: dict[str, str]


def _item(found: ContinueEntry) -> ContinueItemOut:
    return ContinueItemOut(
        work_id=found.work_id, title=found.title, unit_id=found.unit_id, number=found.number,
        unit_title=found.unit_title, state=found.state, target=found.target,
        approximate=found.approximate, carrier_type=found.carrier_type,
        carrier_id=found.carrier_id, open_url=None if found.carrier_type else web_address(
            found.open_url
        ),
    )  # fmt: skip


def web_address(value: str | None) -> str | None:
    """An address the browser may be sent to: http or https, with no `user:pass@` part (a secret
    is never given back to the browser), else None."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port  # raises for a port that is not a number from 0 to 65535
    except ValueError:
        return None  # an address nobody can open is no link: the home is never an error
    if parts.scheme not in ("http", "https") or not host or port == 0:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


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
    carrier_type: (
        str  # the file it was written on: an EPUB read on a device is on its exported file
    )
    carrier_id: str
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


class ReadMarkOut(BaseModel):
    """One read mark as the screen that undoes them shows it: whose it is, and the Unit it
    marks."""

    user_id: str
    username: str
    unit_id: str
    number: str
    title: str | None


class ReadMarksOut(BaseModel):
    items: list[ReadMarkOut]


class UndoOut(BaseModel):
    undone: int  # how many marks were undone: 0 is the answer for nothing to undo


class ReaderSettingsOut(BaseModel):
    """What the reader reads with. `direction` is `None` when the profile chose nothing: the
    publication's own direction wins (the Œuvre's, ADR-0055)."""

    direction: Literal["ltr", "rtl", "webtoon"] | None
    comfort: dict[str, str]


class SettingIn(BaseModel):
    """One choice, with the portée the reader was asked for: `device` names the device the choice
    belongs to (« pour cet appareil »), `everywhere` names none (« partout »). The profile is the
    session's: no field names one."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["direction", "comfort"]
    key: str = Field(min_length=1, max_length=64)  # a series, or `default`, or a comfort's name
    value: str = Field(min_length=1, max_length=64)
    scope: Literal["device", "everywhere"]
    device_id: str | None = Field(default=None, min_length=1, max_length=64)


def _stored(found: StoredPosition) -> StoredOut:
    return StoredOut(
        id=found.id, carrier_type=found.carrier_type, carrier_id=found.carrier_id,
        locator=found.locator, client_ts=found.client_ts,
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


def _admin_session(principal: Principal) -> None:
    """The door of the administrator: a real session of the administrator, nothing else."""
    _own_session(principal)
    if principal.role != "admin":
        raise ProblemError("forbidden")


def _settings(found: Effective) -> ReaderSettingsOut:
    return ReaderSettingsOut(
        direction=found.direction if found.direction in DIRECTIONS else None,  # type: ignore[arg-type]
        comfort=dict(found.comfort),
    )


def build_reader_router(
    get_service: Callable[[], PublicationService],
    get_positions: Callable[[], ReaderPositions],
    get_settings: Callable[[], ReaderSettings],
    get_continue: Callable[[], ContinueReading],
    get_marks: Callable[[], ReadMarks],
) -> APIRouter:
    router = APIRouter(prefix="/api/reader", tags=["reader"])

    @router.get("/read-marks", operation_id="reader_read_marks", response_model=ReadMarksOut)
    def marks_list(
        response: Response,
        principal: Session,
        edition_id: Annotated[str, Query(max_length=26)],
    ) -> ReadMarksOut:
        """The read marks on the Units of an Edition, for the screen that undoes them: the
        profile's own, and for an administrator the child profiles'. Another member's marks are
        never listed; an Edition the profile may not see answers like one with no mark."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        if not is_ulid(edition_id):
            return ReadMarksOut(items=[])
        return ReadMarksOut(
            items=[
                ReadMarkOut(
                    user_id=m.user_id,
                    username=m.username,
                    unit_id=m.unit_id,
                    number=m.number,
                    title=m.title,
                )
                for m in get_marks().of(viewer_id=principal.user_id, edition_id=edition_id)
            ]
        )

    @router.post(
        "/read-marks/{unit_id}/undo",
        operation_id="reader_undo_read_mark",
        response_model=UndoOut,
        responses={404: {"description": "not-found"}},
    )
    def undo_own(unit_id: str, response: Response, principal: Session) -> UndoOut:
        """Undo the profile's own read mark on the Unit: a dated line of history, nothing
        deleted. A Unit the profile may not see, and a mark that is not there, answer alike."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        if not is_ulid(unit_id):
            raise ProblemError("not_found", headers=_ABSENT)
        return UndoOut(
            undone=get_marks().undo(
                viewer_id=principal.user_id, subject_id=principal.user_id, unit_id=unit_id
            )
        )

    @router.post(
        "/read-marks/{unit_id}/undo/for-child/{user_id}",
        operation_id="reader_undo_read_mark_for_child",
        response_model=UndoOut,
        responses={403: {"description": "forbidden"}, 404: {"description": "not-found"}},
    )
    def undo_for_child(
        unit_id: str, user_id: str, response: Response, principal: Session
    ) -> UndoOut:
        """The administrator's door: undo the read mark of a child profile. The mark of another
        adult is not there for the administrator either, and the answer never tells which."""
        _admin_session(principal)
        response.headers["Cache-Control"] = "no-store"
        if not is_ulid(unit_id) or not is_ulid(user_id):
            raise ProblemError("not_found", headers=_ABSENT)
        return UndoOut(
            undone=get_marks().undo(
                viewer_id=principal.user_id, subject_id=user_id, unit_id=unit_id
            )
        )

    @router.get("/continue", operation_id="reader_continue", response_model=ContinueOut)
    def continue_reading(
        response: Response,
        principal: Session,
        limit: Annotated[int, Query(ge=1, le=MAX_ENTRIES)] = DEFAULT_ENTRIES,
    ) -> ContinueOut:
        """« Reprendre la lecture »: the next Unit of every series under way, for the profile of
        the session only (the progress is hers, the filter is the one of every surface)."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        entries = get_continue().entries(principal.user_id, limit)
        handed: dict[str, str] = {}
        servers: set[str] = set()
        for entry in entries:
            if entry.target_id is None or entry.target_id in servers:
                continue
            url = web_address(entry.open_url)
            if not url:
                continue
            servers.add(entry.target_id)
            # the first server of a kind is keyed by the kind; a second one, by its target's id
            handed[entry.target if entry.target not in handed else entry.target_id] = url
        return ContinueOut(
            items=[_item(e) for e in entries],
            open_urls=handed,
        )

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
            raise ProblemError("full_read_not_allowed", headers=_ABSENT) from None
        except ExcerptUnavailableError:  # before its parent: a typed refusal of its own
            raise ProblemError("excerpt_unavailable", headers=_ABSENT) from None
        except NoReadableCopyError:
            raise ProblemError("no_readable_copy", headers=_ABSENT) from None
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
        except ProgressBusyError:
            raise ProblemError("progress_busy", headers=_BUSY) from None
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
        except ProgressBusyError:  # settling writes a position: the lease of the other writers
            raise ProblemError("progress_busy", headers=_BUSY) from None
        except StaleConflictError:
            raise ProblemError("conflict_stale") from None
        except ExcerptWritesNoPositionError:
            raise ProblemError("excerpt_writes_no_position") from None

    @router.get(
        "/settings",
        operation_id="reader_settings",
        response_model=ReaderSettingsOut,
    )
    def settings(
        response: Response,
        principal: Session,
        series_id: Annotated[str | None, Query(max_length=26)] = None,
        device_id: Annotated[str | None, Query(max_length=64)] = None,
    ) -> ReaderSettingsOut:
        """The settings of the profile that asks, for one series and one device: the direction
        pinned to that series (her own default when the series has none) and the comfort of that
        device (the default of every device when it has none)."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        if series_id is not None and not is_ulid(series_id):
            raise ProblemError("validation_failed")
        found = get_settings().resolve(
            user_id=principal.user_id, series_id=series_id, device_id=device_id
        )
        return _settings(found)

    @router.put(
        "/settings",
        operation_id="reader_put_settings",
        response_model=ReaderSettingsOut,
        responses={
            422: {"description": "unknown-choice|settings-limit-reached"},
        },
    )
    def put_settings(body: SettingIn, response: Response, principal: Session) -> ReaderSettingsOut:
        """Write one choice with its portée — « pour cet appareil » (`device`, which names the
        device) or « partout » (`everywhere`, which names none) — and answer what is effective
        now. Only the reader's own settings exist here; a row never widens what parental control
        hides, and a series the profile cannot see answers like one that is not there. Beyond
        the bound of rows a profile may hold, the write of a new place is a typed refusal."""
        _own_session(principal)
        response.headers["Cache-Control"] = "no-store"
        try:
            get_settings().set(
                user_id=principal.user_id, kind=body.kind, key=body.key, value=body.value,
                scope=body.scope, device_id=body.device_id,
            )  # fmt: skip
        except (InvalidSettingError, DeviceScopeError):
            raise ProblemError("validation_failed") from None
        except SettingsLimitError:
            raise ProblemError("settings_limit_reached") from None
        found = get_settings().resolve(
            user_id=principal.user_id,
            series_id=body.key if body.kind == "direction" and body.key != "default" else None,
            device_id=body.device_id,
        )
        return _settings(found)

    return router
