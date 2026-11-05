"""What a cycle may ask for, and how much (EF-EXP-053, EF-BIB-044, EF-EXP-018).

Per-cycle caps count Units, per connected service (50) and per `limit_key` (10); what does not fit
waits for the next cycle. A Unit is requestable only if it is absent (I20, whatever the carrier),
live at an active source, not suppressed ("stop following this chapter"), not acknowledged as a
gap, and without a ticket.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from shelfhold.acquisition.domain.watermark import Watermark

DEFAULT_SERVICE_CAP = 50
DEFAULT_KEY_CAP = 10

Mode = Literal["follow", "gaps", "all"]


class RequestBudget:
    """One cycle's allowance. `take` grants as many of the asked Units as still fit."""

    def __init__(
        self, service_cap: int = DEFAULT_SERVICE_CAP, key_cap: int = DEFAULT_KEY_CAP
    ) -> None:
        self._service = service_cap
        self._key_cap = key_cap
        self._used: dict[str, int] = {}

    @property
    def remaining(self) -> int:
        return self._service

    def room(self, limit_key: str, *, series_cap: int | None = None) -> int:
        room = min(self._service, self._key_cap - self._used.get(limit_key, 0))
        return max(0, room if series_cap is None else min(room, series_cap))

    def take(self, limit_key: str, units: int, *, series_cap: int | None = None) -> int:
        granted = max(0, min(units, self.room(limit_key, series_cap=series_cap)))
        self._service -= granted
        self._used[limit_key] = self._used.get(limit_key, 0) + granted
        return granted


@dataclass(frozen=True)
class KnownUnit:
    unit_id: str
    ordinal: int  # segment ordinal
    number: Decimal | None  # None: an unnumbered extra, which no watermark can place
    present: bool = False
    suppressed: bool = False
    acknowledged: bool = False  # a gap the admin accepted (EF-BIB-056)
    ticketed: bool = False
    live: bool = True


def _key(unit: KnownUnit) -> tuple[int, Decimal]:
    return (unit.ordinal, unit.number if unit.number is not None else Decimal(0))


def requestable(units: Sequence[KnownUnit], mark: Watermark | None, mode: Mode) -> list[KnownUnit]:
    """The Units to ask for, oldest first.

    `follow`: strictly beyond the watermark (none while there is no watermark). `gaps`: at or below
    it and past the first Unit held (a hole inside what is held). `all`: the whole history at or
    below it (everything, when there is no watermark yet).
    """
    numbered = [u for u in units if u.number is not None]
    held = [_key(u) for u in numbered if u.present]
    first_held = min(held) if held else None

    def beyond(unit: KnownUnit) -> bool:
        return mark is not None and mark.is_beyond(ordinal=unit.ordinal, number=_key(unit)[1])

    chosen: list[KnownUnit] = []
    for unit in numbered:
        if unit.present or unit.suppressed or unit.acknowledged or unit.ticketed or not unit.live:
            continue
        if mode == "follow":
            ok = mark is not None and beyond(unit)
        elif mode == "gaps":
            ok = first_held is not None and _key(unit) > first_held and not (mark and beyond(unit))
        else:
            ok = mark is None or not beyond(unit)
        if ok:
            chosen.append(unit)
    return sorted(chosen, key=_key)
