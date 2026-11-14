"""What the add sheet chose for a follow (M3 L42, EF-UX-006): which Units it brings, and where.

`all` and `new_only` bring every numbered Unit the library does not hold yet (at the start nothing
is held, so they ask for the same Units; the estimate of `new_only` is the one that leaves out what
is held). `from_number` brings the Units from that number on, in the unnamed numbering segment.
A follow made without a choice (the host CLI, a series added in the service) has no intent, and the
watermark stays the highest chapter visible: the first cycle asks for what is new,
never the archive.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, get_args

from shelfhold.acquisition.domain.watermark import Watermark

FollowMode = Literal["all", "from_number", "new_only"]
FOLLOW_MODES: tuple[str, ...] = get_args(FollowMode)

_BEFORE_EVERYTHING = Watermark(0, Decimal(-1))
_ONE_STEP = Decimal("0.001")  # below any number a source gives, so that N itself is beyond


@dataclass(frozen=True)
class FollowIntent:
    mode: FollowMode
    from_number: str | None = None
    shelf_id: str | None = None

    def watermark(self) -> Watermark:
        """The mark the follow starts from: a Unit beyond it is asked for."""
        if self.mode != "from_number":
            return _BEFORE_EVERYTHING
        number = parse_number(self.from_number)
        if number is None:
            raise ValueError("from_number is not a number")
        return Watermark(0, number - _ONE_STEP)


def parse_number(raw: str | None) -> Decimal | None:
    try:
        number = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None
