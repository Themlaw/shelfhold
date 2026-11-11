"""Availability of a Unit as a source says it: an open vocabulary (EF-BIB-009, D20).

The contract names six values; a source may say another one (`early_access`, a region lock).
That value is never refused and never trusted: the Unit is `locked` (not exported), and what the
source said is kept, bounded, in `availability_detail`. Nothing here ever reads as `free`.
"""

from dataclasses import dataclass
from typing import Final

KNOWN_AVAILABILITIES: Final = ("free", "purchased", "expiring", "locked", "stubbed", "removed")
DETAIL_MAX: Final = 64


@dataclass(frozen=True)
class Availability:
    value: str  # one of KNOWN_AVAILABILITIES
    detail: str | None = None  # what an unknown value said, bounded


def normalize_availability(raw: str | None) -> Availability:
    """`None` is a source that said nothing (the Unit is `free`); an unknown or empty string is
    `locked`, with the cleaned text as the detail."""
    if raw is None:
        return Availability("free")
    cleaned = "".join(ch for ch in raw.strip().lower() if ch.isprintable())
    if cleaned in KNOWN_AVAILABILITIES:
        return Availability(cleaned)
    return Availability("locked", cleaned[:DETAIL_MAX] or None)
