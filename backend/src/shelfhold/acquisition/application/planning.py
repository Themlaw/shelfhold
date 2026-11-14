"""Reading what a cycle may ask for, from the database (EF-EXP-053, EF-BIB-044)."""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.acquisition.domain.requests import KnownUnit, Mode, requestable
from shelfhold.acquisition.domain.watermark import Watermark
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.library.public import readable_unit_ids

# A ticket in these states is not "asked for": the Unit may be asked again.
_NOT_ASKED = ("cancelled", "closed_by_purge")


def watermark_of(connection: Connection, edition_id: str) -> Watermark | None:
    follow = acquisition.series_follow
    row = connection.execute(
        sa.select(follow.c.follow_from_segment, follow.c.follow_from_number).where(
            follow.c.edition_id == edition_id
        )
    ).one_or_none()
    if row is None or row.follow_from_number is None:
        return None
    return Watermark.of(row.follow_from_segment or "", row.follow_from_number)


NOTHING_YET = Watermark(0, Decimal(-1))  # a series whose first look held only extras


def set_watermark(
    connection: Connection, edition_id: str, mark: Watermark, *, baseline_at: datetime | None = None
) -> None:
    """`baseline_at`: the first look set it; extras seen up to then are not "new" (see below)."""
    follow = acquisition.series_follow
    connection.execute(
        sa.update(follow)
        .where(follow.c.edition_id == edition_id)
        .values(
            follow_from_segment=mark.segment,
            follow_from_number=format(mark.number, "f"),
            follow_from_norm_by="number@1",
            updated_at=baseline_at,
        )
    )


def highest_numbered(connection: Connection, edition_id: str) -> Watermark | None:
    """The most advanced numbered Unit known: the baseline of a series followed from now on."""
    u = catalog.unit
    rows = connection.execute(
        sa.select(u.c.number_segment, u.c.number_segment_ordinal, u.c.number_norm).where(
            u.c.edition_id == edition_id, u.c.track != "unnumbered"
        )
    ).all()
    if not rows:
        return NOTHING_YET
    top = max(rows, key=lambda r: (r.number_segment_ordinal, Decimal(r.number_norm)))
    return Watermark(top.number_segment_ordinal, Decimal(top.number_norm), top.number_segment)


def known_units(connection: Connection, edition_id: str) -> list[KnownUnit]:
    u, v, b = catalog.unit, catalog.unit_variant, acquisition.source_binding
    live = (
        sa.select(v.c.unit_id)
        .join(b, b.c.id == v.c.source_binding_id)
        .join(u, u.c.id == v.c.unit_id)
        .where(v.c.removed_at.is_(None), b.c.active, u.c.edition_id == edition_id)
        .distinct()
    )
    live_ids = set(connection.execute(live).scalars())
    r, ru = acquisition.download_request, acquisition.download_request_unit
    ticketed = set(
        connection.execute(
            sa.select(ru.c.unit_id)
            .join(r, r.c.id == ru.c.download_request_id)
            .join(u, u.c.id == ru.c.unit_id)
            .where(
                r.c.state.notin_(_NOT_ASKED), ru.c.state != "missing", u.c.edition_id == edition_id
            )
        ).scalars()
    )
    present = readable_unit_ids(connection, edition_id)
    since = connection.execute(
        sa.select(acquisition.series_follow.c.updated_at).where(
            acquisition.series_follow.c.edition_id == edition_id
        )
    ).scalar()
    first_seen = {
        row.unit_id: row.seen
        for row in connection.execute(
            sa.select(v.c.unit_id, sa.func.min(v.c.first_seen_at).label("seen"))
            .join(u, u.c.id == v.c.unit_id)
            .where(u.c.edition_id == edition_id)
            .group_by(v.c.unit_id)
        )
    }
    ack = acquisition.gap_ack
    acked = {
        (a.number_segment, a.number_norm)
        for a in connection.execute(
            sa.select(ack.c.number_segment, ack.c.number_norm).where(ack.c.edition_id == edition_id)
        )
    }
    rows = connection.execute(
        sa.select(
            u.c.id, u.c.number_segment, u.c.number_segment_ordinal, u.c.number_norm,
            u.c.track, u.c.follow_suppressed,
        ).where(u.c.edition_id == edition_id)
    )  # fmt: skip
    return [
        KnownUnit(
            unit_id=row.id,
            ordinal=row.number_segment_ordinal,
            number=None if row.track == "unnumbered" else Decimal(row.number_norm),
            present=row.id in present,
            suppressed=bool(row.follow_suppressed),
            acknowledged=(row.number_segment, row.number_norm) in acked,
            ticketed=row.id in ticketed,
            live=row.id in live_ids,
            new_extra=row.track == "unnumbered"
            and (since is None or (first_seen.get(row.id) or since) > since),
        )
        for row in rows
    ]


def pending(connection: Connection, edition_id: str, mode: Mode) -> list[KnownUnit]:
    return requestable(
        known_units(connection, edition_id), watermark_of(connection, edition_id), mode
    )


def held_numbers(
    connection: Connection, source_key: str, remote_id: str
) -> set[tuple[int, Decimal]]:
    """`(segment ordinal, number)` of the numbered Units the library already holds of the series
    this source item is bound to (none when the library has never seen the series): what a
    follow has no need to ask for (I20)."""
    item, binding = acquisition.source_item, acquisition.source_binding
    editions = set(
        connection.execute(
            sa.select(binding.c.edition_id)
            .join(item, item.c.id == binding.c.source_item_id)
            .where(item.c.source_key == source_key, item.c.remote_id == remote_id)
        ).scalars()
    )
    return {
        (unit.ordinal, unit.number)
        for edition_id in editions
        for unit in known_units(connection, edition_id)
        if unit.present and unit.number is not None
    }
