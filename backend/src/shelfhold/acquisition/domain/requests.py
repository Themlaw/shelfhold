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
        self, service_cap: int = DEFAULT_SERVICE_CAP, key_cap: int = DEFAULT_KEY_CAP,
        *, held: dict[str, int] | None = None,
    ) -> None:  # fmt: skip
        self._service = service_cap
        self._key_cap = key_cap
        self._used: dict[str, int] = {}
        self._held = dict(held or {})  # keys kept free this cycle for a waiting volume
        self.holds: dict[str, int] = {}  # keys to keep free next cycle (one volume per key)

    @property
    def remaining(self) -> int:
        return self._service

    @property
    def capacity(self) -> int:
        """The most Units one range can ever be granted in a cycle."""
        return min(self._service, self._key_cap)

    def room(self, limit_key: str, *, series_cap: int | None = None, claiming: bool = False) -> int:
        """What is left for `limit_key`; what a waiting volume holds is left only to the volume
        (`claiming`)."""
        held_here = 0 if claiming else self._held.get(limit_key, 0)
        held_all = 0 if claiming else sum(self._held.values()) - held_here
        room = min(
            self._service - held_all, self._key_cap - self._used.get(limit_key, 0) - held_here
        )
        return max(0, room if series_cap is None else min(room, series_cap))

    def hold(self, limit_key: str, units: int) -> None:
        """A volume that did not fit keeps `units` of the key free for the next cycle: the
        volume goes first there, and only one waits per key."""
        self.holds.setdefault(limit_key, min(units, self.capacity))

    def take(
        self, limit_key: str, units: int, *, series_cap: int | None = None, claiming: bool = False
    ) -> int:
        granted = max(0, min(units, self.room(limit_key, series_cap=series_cap, claiming=claiming)))
        if claiming:
            self._held.pop(limit_key, None)
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
    new_extra: bool = False  # an unnumbered extra that appeared after the first look


def _key(unit: KnownUnit) -> tuple[int, Decimal]:
    return (unit.ordinal, unit.number if unit.number is not None else Decimal(0))


def _order(unit: KnownUnit) -> tuple[int, int, Decimal]:
    return (unit.number is None, *_key(unit))


def _blocked(unit: KnownUnit) -> bool:
    return unit.present or unit.suppressed or unit.acknowledged or unit.ticketed or not unit.live


def requestable(units: Sequence[KnownUnit], mark: Watermark | None, mode: Mode) -> list[KnownUnit]:
    """The Units to ask for, oldest first.

    `follow`: strictly beyond the watermark (none while there is no watermark). `gaps`: at or below
    it and past the first Unit held (a hole inside what is held). `all`: the whole history at or
    below it (everything, when there is no watermark yet).
    """
    numbered = [u for u in units if u.number is not None]
    extras = [
        u
        for u in units
        if u.number is None and mode == "follow" and u.new_extra and mark is not None
    ]
    held = [_key(u) for u in numbered if u.present]
    first_held = min(held) if held else None

    def beyond(unit: KnownUnit) -> bool:
        return mark is not None and mark.is_beyond(ordinal=unit.ordinal, number=_key(unit)[1])

    chosen: list[KnownUnit] = []
    for unit in numbered:
        if _blocked(unit):
            continue
        if mode == "follow":
            ok = mark is not None and beyond(unit)
        elif mode == "gaps":
            ok = first_held is not None and _key(unit) > first_held and not (mark and beyond(unit))
        else:
            ok = mark is None or not beyond(unit)
        if ok:
            chosen.append(unit)
    chosen += [u for u in extras if not _blocked(u)]
    return sorted(chosen, key=_order)
