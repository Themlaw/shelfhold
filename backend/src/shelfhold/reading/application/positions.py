"""The positions of the integrated reader (M5 L07, EF-LEC-007, EF-LEC-014, ENF-SEC-010).

What the reader writes is an **anchor** (`reading_position`, source `device`) in the locator v1 of
ADR-0055, on a kept copy (`content`: a Work that is not exported) or on an exported file
(`export_item`); the progress of a Unit is recomputed from it by `reading_progress`, never sent.

* **Who.** The profile is the session's: a request names a carrier, never a profile. A carrier the
  profile may not see (a child above her ceiling, another profile's reserved shelf) is the answer
  of one that does not exist (`UnknownCarrierError`), on every door here.
* **An excerpt writes nothing.** A call that says `excerpt`, one that carries an excerpt ticket and
  a carrier that can only be read as an excerpt (an exported file: D-P1) are refused with nothing
  stored, no history and no proposal, so that nothing of it can reach Kavita or kosync.
* **Where the reader is.** The latest `client_ts`, bounded by `server_ts` (`reading.domain.merge`):
  an offline position is dated by when it was made, never later than the server's date, and a
  date more than `CLIENT_SKEW` ahead of the server's clock is refused. Going back is allowed; the
  history (bounded by `HISTORY_LIMIT`) keeps the rest, and a Unit that was read stays read.
* **A divergence.** When another device's latest position is further than the winner by D8's
  threshold, `reading_conflict` records it once; the reader chooses (`resolve`) and what she
  dismissed is not proposed again until that device speaks again.
* **Another filter.** A position written under another version of the filter, or on a content that
  was replaced, is restored as it was written and marked `approximate`, never moved in silence.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa
import structlog

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import reading
from shelfhold.reading.application.content_access import ContentAccess, NoReadableCopyError
from shelfhold.reading.application.progress import (
    HISTORY_LIMIT,
    _current_hash,
    check_carrier,
    insert_position,
)
from shelfhold.reading.domain.locator import (
    InvalidLocatorV1Error,
    current_content_filter,
    parse_locator_v1,
)
from shelfhold.reading.domain.merge import Reported, bounded_ts, conflicting, winner

log = structlog.get_logger(__name__)

__all__ = [
    "CLIENT_SKEW",
    "ClientTsInFutureError",
    "Conflict",
    "Current",
    "ExcerptWritesNoPositionError",
    "InvalidTicketError",
    "ReaderPositions",
    "StoredPosition",
    "UnknownConflictError",
]

CLIENT_SKEW: Final = timedelta(minutes=5)  # a clock a little ahead is bounded, a far one refused
Choice = Literal["winner", "loser"]


class ExcerptWritesNoPositionError(Exception):
    """An excerpt (or a carrier readable only as one) writes no position."""


class ClientTsInFutureError(ValueError):
    """`client_ts` is more than `CLIENT_SKEW` ahead of the server's clock."""


class InvalidTicketError(ValueError):
    """The ticket is not this profile's, not for this carrier, expired or not ours."""


class UnknownConflictError(LookupError):
    """No open proposal of this profile has this identifier (also for another profile's)."""


@dataclass(frozen=True)
class StoredPosition:
    id: str
    carrier_type: str
    carrier_id: str
    locator: dict[str, Any]
    client_ts: datetime
    server_ts: datetime
    device_id: str | None
    approximate: bool


@dataclass(frozen=True)
class Conflict:
    id: str
    winner: StoredPosition
    loser: StoredPosition
    created_at: datetime


@dataclass(frozen=True)
class Current:
    position: StoredPosition | None
    conflict: Conflict | None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ReaderPositions:
    def __init__(
        self, engine: sa.Engine, access: ContentAccess, now: Callable[[], datetime]
    ) -> None:
        self._engine, self._access, self._now = engine, access, now

    # -- writing ---------------------------------------------------------------------------

    def write(
        self,
        *,
        user_id: str,
        carrier_type: str,
        carrier_id: str,
        locator: object,
        client_ts: datetime,
        device_id: str | None,
        scope: str = "full",
        ticket: str | None = None,
    ) -> Current:
        """Record where `user_id` is, as herself; returns where she is now and any proposal.

        Raises `InvalidLocatorV1Error`, `ClientTsInFutureError`, `InvalidTicketError`,
        `UnknownCarrierError` (absent and hidden alike), `ExcerptWritesNoPositionError`,
        `NoReadableCopyError` (a content with no hash cannot anchor a position)."""
        parsed = parse_locator_v1(locator)
        now = self._now()
        if _utc(client_ts) > now + CLIENT_SKEW:
            raise ClientTsInFutureError
        ticket_scope = None
        if ticket is not None:
            ticket_scope = self._access.scope_of(
                ticket, user_id=user_id, carrier_type=carrier_type, carrier_id=carrier_id
            )
            if ticket_scope is None:
                raise InvalidTicketError
        resolved = self._access.resolve(user_id, carrier_type, carrier_id)  # absent = hidden
        if scope == "excerpt" or ticket_scope == "excerpt" or not resolved.full_allowed:
            raise ExcerptWritesNoPositionError
        if (parsed.kind == "cfi") != (resolved.format == "epub"):
            raise InvalidLocatorV1Error("the locator is not one of this format")
        with write_transaction(self._engine) as connection:
            check_carrier(connection, user_id, carrier_type, carrier_id)  # again, in the write
            content_hash = _current_hash(connection, carrier_type, carrier_id)
            if content_hash is None:
                raise NoReadableCopyError
            insert_position(
                connection, user_id=user_id, carrier_type=carrier_type, carrier_id=carrier_id,
                content_hash=content_hash, locator=parsed.stored(), percent=parsed.progression,
                client_ts=_utc(client_ts), device_id=device_id, source="device", now=now,
                traverse=parsed.kind == "page",  # a place in a reflowable text proves no reading
            )  # fmt: skip
            self._propose(connection, user_id, carrier_type, carrier_id, now)
            return self._current(connection, user_id, carrier_type, carrier_id)

    def _propose(
        self,
        connection: sa.Connection,
        user_id: str,
        carrier_type: str,
        carrier_id: str,
        now: datetime,
    ) -> None:
        """Record the divergence of two devices, once (D8), if there is one."""
        table = reading.reading_conflict
        mine = sa.and_(
            table.c.user_id == user_id,
            table.c.carrier_type == carrier_type,
            table.c.carrier_id == carrier_id,
        )
        if connection.execute(
            sa.select(sa.literal(1)).where(mine, table.c.resolved_at.is_(None)).limit(1)
        ).first():
            return  # one is open: proposed once, until she chooses
        settled = connection.execute(
            sa.select(sa.func.max(table.c.resolved_at)).where(mine)
        ).scalar()
        since = None if settled is None else _utc(settled)
        rows = self._v1_rows(connection, user_id, carrier_type, carrier_id)
        if not rows:
            return
        reports = {r.id: _reported(r) for r in rows}
        won = winner(list(reports.values()))
        latest_by_device: dict[str | None, Reported] = {}
        for report in reports.values():
            if report.position_id == won.position_id:
                continue
            if since is not None and _utc(report.server_ts) <= since:
                continue  # what she dismissed is not asked again until that device speaks again
            kept = latest_by_device.get(report.device_id)
            if kept is None or winner([kept, report]).position_id == report.position_id:
                latest_by_device[report.device_id] = report
        lost = conflicting(won, list(latest_by_device.values()))
        if lost is None:
            return
        connection.execute(
            sa.insert(table).values(
                id=new_ulid(), user_id=user_id, carrier_type=carrier_type, carrier_id=carrier_id,
                winner_position_id=won.position_id, loser_position_id=lost.position_id,
            )
        )  # fmt: skip
        log.info("reading.conflict_proposed", carrier_type=carrier_type)

    # -- reading ---------------------------------------------------------------------------

    def current(self, *, user_id: str, carrier_type: str, carrier_id: str) -> Current:
        with self._engine.connect() as connection:
            check_carrier(connection, user_id, carrier_type, carrier_id)
            return self._current(connection, user_id, carrier_type, carrier_id)

    def history(
        self, *, user_id: str, carrier_type: str, carrier_id: str, limit: int = HISTORY_LIMIT
    ) -> list[StoredPosition]:
        """The positions of the profile on this file, the latest first (`client_ts` bounded by
        `server_ts`), at most `HISTORY_LIMIT`."""
        with self._engine.connect() as connection:
            check_carrier(connection, user_id, carrier_type, carrier_id)
            rows = self._v1_rows(connection, user_id, carrier_type, carrier_id)
            now_hash = _current_hash(connection, carrier_type, carrier_id)
        rows.sort(key=lambda r: (bounded_ts(_utc(r.client_ts), _utc(r.server_ts)), r.id),
                  reverse=True)  # fmt: skip
        return [_stored(r, now_hash) for r in rows[: max(0, min(limit, HISTORY_LIMIT))]]

    def _v1_rows(
        self, connection: sa.Connection, user_id: str, carrier_type: str, carrier_id: str
    ) -> list[Any]:
        table = reading.reading_position
        return list(
            connection.execute(
                sa.select(table)
                .where(
                    table.c.user_id == user_id,
                    table.c.carrier_type == carrier_type,
                    table.c.carrier_id == carrier_id,
                    table.c.source == "device",
                    sa.func.json_extract(table.c.locator, "$.v") == 1,
                )
                .order_by(table.c.server_ts.desc(), table.c.id.desc())
                .limit(HISTORY_LIMIT)
            )
        )

    def _current(
        self, connection: sa.Connection, user_id: str, carrier_type: str, carrier_id: str
    ) -> Current:
        rows = self._v1_rows(connection, user_id, carrier_type, carrier_id)
        now_hash = _current_hash(connection, carrier_type, carrier_id)
        by_id = {r.id: r for r in rows}
        position = None
        if rows:
            best = winner([_reported(r) for r in rows])
            position = _stored(by_id[best.position_id], now_hash)
        return Current(position, self._open_conflict(connection, user_id, carrier_type, carrier_id))

    def _open_conflict(
        self, connection: sa.Connection, user_id: str, carrier_type: str, carrier_id: str
    ) -> Conflict | None:
        table = reading.reading_conflict
        row = connection.execute(
            sa.select(table).where(
                table.c.user_id == user_id,
                table.c.carrier_type == carrier_type,
                table.c.carrier_id == carrier_id,
                table.c.resolved_at.is_(None),
            )
        ).first()
        return None if row is None else self._conflict(connection, row)

    def _conflict(self, connection: sa.Connection, row: Any) -> Conflict | None:
        position = reading.reading_position
        now_hash = _current_hash(connection, row.carrier_type, row.carrier_id)
        found = {
            r.id: r
            for r in connection.execute(
                sa.select(position).where(
                    position.c.id.in_([row.winner_position_id, row.loser_position_id]),
                    position.c.user_id == row.user_id,
                )
            )
        }
        if row.winner_position_id not in found or row.loser_position_id not in found:
            return None  # a side is gone: there is nothing left to choose between
        return Conflict(
            row.id, _stored(found[row.winner_position_id], now_hash),
            _stored(found[row.loser_position_id], now_hash), _utc(row.created_at),
        )  # fmt: skip

    # -- settling --------------------------------------------------------------------------

    def resolve(
        self, *, user_id: str, conflict_id: str, choice: Choice, device_id: str | None = None
    ) -> Current:
        """The reader chose. `winner` keeps the latest and dismisses what the other device said;
        `loser` resumes where the other device was: that place is written again as the latest
        position (a new anchor, the history is kept). An open proposal of hers, else
        `UnknownConflictError` (also for another profile's, and once settled)."""
        table = reading.reading_conflict
        now = self._now()
        with write_transaction(self._engine) as connection:
            row = connection.execute(
                sa.select(table).where(
                    table.c.id == conflict_id,
                    table.c.user_id == user_id,
                    table.c.resolved_at.is_(None),
                )
            ).first()
            if row is None:
                raise UnknownConflictError
            check_carrier(connection, user_id, row.carrier_type, row.carrier_id)
            if choice == "loser":
                position = reading.reading_position
                sides = {
                    r.id: r
                    for r in connection.execute(
                        sa.select(position).where(
                            position.c.id.in_([row.winner_position_id, row.loser_position_id]),
                            position.c.user_id == user_id,
                        )
                    )
                }
                lost = sides.get(row.loser_position_id)
                if lost is not None:
                    insert_position(
                        connection, user_id=user_id, carrier_type=row.carrier_type,
                        carrier_id=row.carrier_id, content_hash=lost.content_hash,
                        locator=lost.locator, percent=lost.percent, client_ts=now,
                        device_id=device_id
                        or (sides[row.winner_position_id].device_id
                            if row.winner_position_id in sides else lost.device_id),
                        source="device", now=now, traverse=False,
                    )  # fmt: skip
            connection.execute(
                sa.update(table).where(table.c.id == row.id).values(choice=choice, resolved_at=now)
            )
            return self._current(connection, user_id, row.carrier_type, row.carrier_id)


def _reported(row: Any) -> Reported:
    locator = row.locator
    page = locator.get("type") == "page"
    return Reported(
        row.id, row.device_id, float(row.percent if row.percent is not None else 0.0),
        _utc(row.client_ts), _utc(row.server_ts), row.content_hash,
        locator.get("index") if page else None, locator.get("count") if page else None,
    )  # fmt: skip


def _stored(row: Any, now_hash: str | None) -> StoredPosition:
    locator = dict(row.locator)
    filtered_otherwise = locator.get("type") == "cfi" and locator.get("filter") != (
        current_content_filter()
    )
    replaced = now_hash is not None and row.content_hash != now_hash
    return StoredPosition(
        row.id, row.carrier_type, row.carrier_id, locator, _utc(row.client_ts),
        _utc(row.server_ts), row.device_id, filtered_otherwise or replaced,
    )  # fmt: skip
