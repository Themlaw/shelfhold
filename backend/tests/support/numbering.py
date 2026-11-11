"""The total order of Units and the `follow_from` watermark as plain functions: the reference the
tests compare the SQL ordering and `Watermark` (acquisition) with. Production orders in SQL."""

from datetime import UTC, datetime
from decimal import Decimal

from shelfhold.catalog.domain.numbering import Track, UnitNumber

_TRACK_RANK = {track: rank for rank, track in enumerate(Track)}


def unit_sort_key(
    unit: UnitNumber, published_at: datetime | None = None, remote_unit_id: str = ""
) -> tuple[int, int, Decimal, int, int, tuple[int, datetime], str]:
    """Total sort key; a missing date sorts after any known date."""
    if published_at is None:
        when = (1, datetime.min.replace(tzinfo=UTC))
    else:
        aware = published_at if published_at.tzinfo else published_at.replace(tzinfo=UTC)
        when = (0, aware)
    return (
        _TRACK_RANK[unit.track],
        unit.segment_ordinal,
        unit.sort,
        unit.part,
        unit.source_order,
        when,
        remote_unit_id,
    )


def follow_from(unit: UnitNumber) -> tuple[int, Decimal]:
    """`follow_from` watermark: the pair `(segment_ordinal, number_sort)` (EF-BIB-050)."""
    return (unit.segment_ordinal, unit.sort)


def is_beyond(unit: UnitNumber, mark: tuple[int, Decimal]) -> bool:
    """True if the Unit is strictly beyond the watermark."""
    return follow_from(unit) > mark
