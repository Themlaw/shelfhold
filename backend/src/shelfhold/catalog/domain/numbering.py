"""Numérotation des Unités (EF-BIB-002, EF-BIB-050, ENF-OPS-021).

Un numéro brut devient `(track, segment, number_norm, part)` plus une clé de tri décimale. Le
normaliseur est versionné (`number@1`) et la valeur brute est gardée. Le tri est total :

    (track, segment_ordinal, number_sort, part, source_order, published_at, remote_unit_id)

Le segment (saison) précède le numéro dans la clé car la place de l'Unité est
`(edition, track, number_segment, number_norm, part)` ; l'ordinal, jamais le texte, ordonne les
segments (`S10` après `S2`). `part` vaut 1 par défaut, si bien que « 10 » et « 10 part 1 » désignent
la même place. Un numéro absent ou illisible tombe dans la piste « non numéroté », rangée par
`source_order`.
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
    """Pistes, dans l'ordre du tri ; `UNNUMBERED` est le repli d'EF-BIB-034."""

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
    """Normalise un numéro brut ; ne lève jamais d'exception."""
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
    """Clé de tri totale ; une date absente passe après toute date connue."""
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
    """Filigrane `follow_from` : le couple `(segment_ordinal, number_sort)` (EF-BIB-050)."""
    return (unit.segment_ordinal, unit.sort)


def is_beyond(unit: UnitNumber, mark: tuple[int, Decimal]) -> bool:
    """Vrai si l'Unité est strictement au-delà du filigrane."""
    return follow_from(unit) > mark
