"""Backfill thresholds (EF-EXP-053): an estimate first, a confirmation above 100 Units or 5 GiB."""

from dataclasses import dataclass
from typing import Literal

CONFIRM_UNITS = 100
CONFIRM_BYTES = 5 * 1024**3
DEFAULT_UNIT_BYTES = 30 * 1024**2  # a chapter archive, when nothing is stored yet to measure

BackfillMode = Literal["gaps", "all"]


def needs_confirmation(*, units: int, size_bytes: int) -> bool:
    return units > CONFIRM_UNITS or size_bytes > CONFIRM_BYTES


@dataclass(frozen=True)
class BackfillEstimate:
    mode: BackfillMode
    units: int
    size_bytes: int
    free_bytes: int
    needs_confirmation: bool
