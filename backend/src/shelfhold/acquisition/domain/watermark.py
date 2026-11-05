"""The follow watermark `(number_segment, number_norm)` and its ordinal (EF-EXP-053, I34).

Segments are compared by their ordinal, never by their text, and the segment comes first: a
chapter of a more recent segment is beyond the watermark whatever its number.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

_SEGMENT = re.compile(r"S(\d{1,4})")


def ordinal_of(segment: str) -> int:
    """`''` is the first (unnamed) segment, 0; `S3` is 3. The normalizer produces nothing else."""
    if segment == "":
        return 0
    match = _SEGMENT.fullmatch(segment)
    if match is None:
        raise ValueError(f"not a numbering segment: {segment!r}")
    return int(match.group(1))


@dataclass(frozen=True)
class Watermark:
    ordinal: int
    number: Decimal
    segment: str = ""

    @classmethod
    def of(cls, segment: str, number: str | Decimal) -> "Watermark":
        return cls(ordinal_of(segment), Decimal(number), segment)

    def is_beyond(self, *, ordinal: int, number: Decimal) -> bool:
        return (ordinal, number) > (self.ordinal, self.number)
