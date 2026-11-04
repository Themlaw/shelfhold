"""Unit numbering (EF-BIB-002, EF-BIB-050, ENF-OPS-021).

A raw number becomes `(track, segment, number_norm, part)` plus a decimal sort key. The normalizer
is versioned (`number@1`) and the raw value is kept. The order is total:

    (track, segment_ordinal, number_sort, part, source_order, published_at, remote_unit_id)

The segment (season) precedes the number in the key because the Unit's place is
`(edition, track, number_segment, number_norm, part)`; the ordinal, never the text, orders segments
(`S10` after `S2`). `part` defaults to 1, so "10" and "10 part 1" are the same place. A missing or
unreadable number falls into the "unnumbered" track, ordered by `source_order`.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

NUMBER_NORMALIZER_NAME = "number"
NUMBER_NORMALIZER_VERSION = 1
NUMBER_NORMALIZER_ID = f"{NUMBER_NORMALIZER_NAME}@{NUMBER_NORMALIZER_VERSION}"


class Track(StrEnum):
    """Tracks in sort order; `UNNUMBERED` is the EF-BIB-034 fallback."""

    MAIN = "main"
    ANNUAL = "annual"
    SPECIAL = "special"
    EXTRA = "extra"
    ONESHOT = "oneshot"
    UNNUMBERED = "unnumbered"


_TRACK_RANK = {track: rank for rank, track in enumerate(Track)}

_TRACK_WORDS: tuple[tuple[re.Pattern[str], Track], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), track)
    for pattern, track in (
        (r"\bannual\b|\bannuel(?:le)?\b|\bjahrbuch\b", Track.ANNUAL),
        (r"\bspecial\b|\bspécial\b|\bhors[\s-]?série\b|\bsp\b", Track.SPECIAL),
        (r"\bone[\s-]?shot\b|\bunitaire\b", Track.ONESHOT),
        (r"\bextras?\b|\bbonus\b|\bomake\b", Track.EXTRA),
    )
)
_SEGMENT = re.compile(
    r"\b(?:s|season|saison|staffel)\s*\.?\s*(\d{1,4})(?:\s*e(?=\d)|\b)", re.IGNORECASE
)
_PART = re.compile(r"\b(?:part|pt|partie|teil)\s*\.?\s*(\d{1,3})\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<!\d)(?<!\d[.,])(\d+(?:[.,]\d+)?)")


@dataclass(frozen=True)
class UnitNumber:
    raw: str
    norm: str
    sort: Decimal
    part: int
    track: Track
    segment: str
    segment_ordinal: int
    source_order: int
    normalized_by: str = NUMBER_NORMALIZER_ID


def _canonical_decimal(token: str) -> Decimal:
    value = Decimal(token.replace(",", "."))
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()


def parse_number(
    raw: str | None, *, source_order: int = 0, track_hint: Track | None = None
) -> UnitNumber:
    """Normalize a raw number; never raises."""
    text = raw or ""
    work = text
    segment, ordinal = "", 0
    if match := _SEGMENT.search(work):
        ordinal = int(match.group(1))
        segment = f"S{ordinal}"
        work = work[: match.start()] + " " + work[match.end() :]
    part = 1
    if match := _PART.search(work):
        part = max(int(match.group(1)), 1)
        work = work[: match.start()] + " " + work[match.end() :]
    track = track_hint
    for pattern, candidate in _TRACK_WORDS:
        if pattern.search(work):
            track = candidate
            work = pattern.sub(" ", work)
            break
    number_match = _NUMBER.search(work)
    if number_match is not None:
        try:
            value = _canonical_decimal(number_match.group(1))
        except InvalidOperation:  # pragma: no cover - the pattern only admits digits
            value = None
    else:
        value = None
    if value is None:
        resolved = track if track is not None else Track.UNNUMBERED
        return UnitNumber(text, "", Decimal(0), part, resolved, segment, ordinal, source_order)
    return UnitNumber(
        text, format(value, "f"), value, part, track or Track.MAIN, segment, ordinal, source_order
    )


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
