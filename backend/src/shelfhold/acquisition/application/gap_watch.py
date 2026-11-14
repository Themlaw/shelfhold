"""Numbering gaps of a followed Edition: found, told once, silenced by an acknowledgement
(EF-BIB-056, EF-INT-006 `series.gap`).

A cycle reads the places it knows after every binding of the Edition has been listed (a place that
is absent from the first rank may be listed by the second: that one is asked by the election, not
reported). What is still missing is written to `series_gap`; only the gaps that were not there at
the last scan are told, one event per series and cycle, never blocking. A gap that is filled or
acknowledged leaves the table, so the same hole is told once, not at each cycle. `gap_ack` says
"this number does not exist".
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.acquisition.application.planning import watermark_of
from shelfhold.acquisition.domain.gaps import Place, find_gaps
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.library.public import readable_unit_ids

__all__ = ["GapWatch", "acknowledge", "unacknowledge"]

log = structlog.get_logger(__name__)

Emit = Callable[[Connection, str, dict[str, object]], None]


def _places(connection: Connection, edition_id: str) -> list[Place]:
    u, v, b = catalog.unit, catalog.unit_variant, acquisition.source_binding
    live = set(
        connection.execute(
            sa.select(v.c.unit_id)
            .join(b, b.c.id == v.c.source_binding_id)
            .join(u, u.c.id == v.c.unit_id)
            .where(v.c.removed_at.is_(None), b.c.active, u.c.edition_id == edition_id)
        ).scalars()
    )
    known = live | readable_unit_ids(connection, edition_id)
    rows = connection.execute(
        sa.select(
            u.c.id, u.c.track, u.c.number_segment, u.c.number_segment_ordinal, u.c.number_norm
        ).where(u.c.edition_id == edition_id, u.c.track != "unnumbered")
    )
    return [
        Place(r.track, r.number_segment, r.number_segment_ordinal, Decimal(r.number_norm))
        for r in rows
        if r.id in known
    ]


def _acknowledged(connection: Connection, edition_id: str) -> frozenset[tuple[str, str, str]]:
    a = acquisition.gap_ack
    return frozenset(
        (r.track, r.number_segment, r.number_norm)
        for r in connection.execute(
            sa.select(a.c.track, a.c.number_segment, a.c.number_norm).where(
                a.c.edition_id == edition_id
            )
        )
    )


class GapWatch:
    def __init__(self, emit: Emit) -> None:
        self._emit = emit

    @staticmethod
    def _state(connection: Connection, edition_id: str) -> tuple[dict[Any, Any], dict[Any, Any]]:
        """The gaps held in `series_gap` and the gaps there are now, each by its key."""
        gaps = find_gaps(
            _places(connection, edition_id),
            watermark_of(connection, edition_id),
            _acknowledged(connection, edition_id),
        )
        table = acquisition.series_gap
        held = {
            (r.track, r.number_segment, r.number_norm): r
            for r in connection.execute(sa.select(table).where(table.c.edition_id == edition_id))
        }
        return held, {(g.track, g.segment, str(g.number)): g for g in gaps}

    def would_change(self, connection: Connection, edition_id: str) -> bool:
        """`scan` would write: a gap was filled or is new."""
        held, keys = self._state(connection, edition_id)
        return held.keys() != keys.keys()

    def scan(self, connection: Connection, edition_id: str, work_id: str, now: datetime) -> int:
        """Bring `series_gap` to what is missing now; the number of gaps told in this call."""
        held, keys = self._state(connection, edition_id)
        table = acquisition.series_gap
        for key in held.keys() - keys.keys():  # filled, acknowledged or renumbered away
            connection.execute(
                sa.delete(table).where(
                    table.c.edition_id == edition_id, table.c.track == key[0],
                    table.c.number_segment == key[1], table.c.number_norm == key[2],
                )
            )  # fmt: skip
        new = [g for key, g in keys.items() if key not in held]
        for gap in new:
            connection.execute(
                sqlite_insert(table)
                .values(
                    edition_id=edition_id, track=gap.track, number_segment=gap.segment,
                    number_norm=str(gap.number), first_seen_at=now, notified_at=now,
                )
                .on_conflict_do_nothing()
            )  # fmt: skip
        if new:
            log.info("follow.gaps_found", edition_id=edition_id, count=len(new))
            self._emit(
                connection, "series.gap",
                {"edition_id": edition_id, "work_id": work_id, "count": len(new)},
            )  # fmt: skip
        return len(new)


def acknowledge(
    connection: Connection, edition_id: str, *, track: str, segment: str, number: str,
    actor_id: str | None, now: datetime,
) -> bool:  # fmt: skip
    """ "This number does not exist": the gap disappears and is not told again. False when it was
    acknowledged already."""
    inserted = connection.execute(
        sqlite_insert(acquisition.gap_ack)
        .values(
            edition_id=edition_id, track=track, number_segment=segment, number_norm=number,
            acked_by=actor_id, acked_at=now,
        )
        .on_conflict_do_nothing()
    ).rowcount  # fmt: skip
    table = acquisition.series_gap
    connection.execute(
        sa.delete(table).where(
            table.c.edition_id == edition_id, table.c.track == track,
            table.c.number_segment == segment, table.c.number_norm == number,
        )
    )  # fmt: skip
    return bool(inserted)


def unacknowledge(
    connection: Connection, edition_id: str, *, track: str, segment: str, number: str
) -> bool:
    a = acquisition.gap_ack
    return bool(
        connection.execute(
            sa.delete(a).where(
                a.c.edition_id == edition_id, a.c.track == track,
                a.c.number_segment == segment, a.c.number_norm == number,
            )
        ).rowcount
    )  # fmt: skip
