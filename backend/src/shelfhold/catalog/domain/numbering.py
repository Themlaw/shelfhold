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
from decimal import Context, Decimal
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
_DECIMAL = r"\d+(?:[.,\u066b]\d+)?"
_NUMBER = re.compile(rf"(?<!\d)(?<!\d[.,\u066b])({_DECIMAL})")
# A number introduced by a chapter/episode marker wins over any other number ("Vol.2 Ch.10" is 10).
_MARKED_NUMBER = re.compile(
    rf"(?:\b(?:ch(?:apter|apitre|ap)?|ep(?:isode)?)|#)\s*\.?\s*(?<!\d)({_DECIMAL})", re.IGNORECASE
)
_NEGATIVE = re.compile(r"(?<!\w)[-\u2212]\s?\d")

MAX_INTEGER_DIGITS = 18
MAX_FRACTION_DIGITS = 6
# Explicit context: every accepted value (at most 18 + 6 digits) is represented exactly.
_CONTEXT = Context(prec=MAX_INTEGER_DIGITS + MAX_FRACTION_DIGITS + 4)


class NumberError(ValueError):
    """A number the normalizer refuses; `code` is `negative`, `too_long` or `too_precise`."""

    def __init__(self, code: str, raw: str) -> None:
        super().__init__(f"{code}: {raw!r}")
        self.code = code
        self.raw = raw


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


def _canonical_decimal(token: str, raw: str) -> Decimal:
    """Exact decimal of a token within the documented bounds.

    The comma is always a decimal separator ("10,5" is 10.5), so "1,000" is 1, not one thousand:
    thousands separators do not occur in chapter numbers, decimal commas do.
    """
    text = token.replace(",", ".").replace("\u066b", ".")
    whole, _, fraction = text.partition(".")
    whole = whole.lstrip("0")
    fraction = fraction.rstrip("0")
    if len(whole) > MAX_INTEGER_DIGITS:
        raise NumberError("too_long", raw)
    if len(fraction) > MAX_FRACTION_DIGITS:
        raise NumberError("too_precise", raw)
    return Decimal(f"{whole or '0'}.{fraction}" if fraction else whole or "0", context=_CONTEXT)


def parse_number(
    raw: str | None, *, source_order: int = 0, track_hint: Track | None = None
) -> UnitNumber:
    """Normalize a raw number.

    Raises `NumberError` for a negative number or one beyond the documented bounds (18 integer
    digits, 6 decimals); anything else unreadable falls back to the unnumbered track.
    """
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
    if _NEGATIVE.search(work):
        raise NumberError("negative", text)
    number_match = _MARKED_NUMBER.search(work) or _NUMBER.search(work)
    value = _canonical_decimal(number_match.group(1), text) if number_match else None
    if value is None:
        resolved = track if track is not None else Track.UNNUMBERED
        return UnitNumber(text, "", Decimal(0), part, resolved, segment, ordinal, source_order)
    return UnitNumber(
        text, format(value, "f"), value, part, track or Track.MAIN, segment, ordinal, source_order
    )
