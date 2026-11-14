"""The « Ajouter » sheet against the disk (EF-BIB-030, ENF-OPS-006): what an addition is estimated
to weigh is compared with the free space less the reserve, and an addition that does not fit is
refused **before any write**, with the three figures and the way out (« Vider la corbeille »).

Pure rules and plain values; the measure of the free space is the caller's (`ops.library`), and the
reserve is the pause mark of the disk guard (`max(5 %, 10 GiB)`): an addition may never be what
takes the disk under the mark that pauses every write queue.
"""

from dataclasses import dataclass
from typing import Final, Protocol

__all__ = [
    "EMPTY_TRASH_ACTION",
    "GIB",
    "AddSpaceRefusedError",
    "SpaceFigures",
    "check_add_fits",
    "reserve_for",
]

GIB: Final = 1024**3
EMPTY_TRASH_ACTION: Final = "empty_trash"  # the action the refusal proposes (never a title)


class _Marks(Protocol):
    def pause_below(self, total: int) -> int: ...


def reserve_for(total_bytes: int, marks: _Marks) -> int:
    """The reserve of a volume: the free space under which the guard pauses the writes."""
    return marks.pause_below(total_bytes)


@dataclass(frozen=True)
class SpaceFigures:
    estimated_bytes: int
    free_bytes: int
    reserve_bytes: int

    @property
    def usable_bytes(self) -> int:
        """What an addition may take: the free space less the reserve, never negative."""
        return max(self.free_bytes - self.reserve_bytes, 0)

    def as_dict(self) -> dict[str, int]:
        return {
            "estimated_bytes": self.estimated_bytes,
            "free_bytes": self.free_bytes,
            "reserve_bytes": self.reserve_bytes,
            "usable_bytes": self.usable_bytes,
        }


class AddSpaceRefusedError(Exception):
    """The estimated size does not fit; nothing was written."""

    code = "library.space_insufficient"
    reclaim_action = EMPTY_TRASH_ACTION

    def __init__(self, figures: SpaceFigures) -> None:
        super().__init__(
            f"{self.code}: {figures.estimated_bytes / GIB:.1f} GiB estimated, "
            f"{figures.free_bytes / GIB:.1f} GiB free, "
            f"{figures.reserve_bytes / GIB:.1f} GiB kept in reserve"
        )
        self.figures = figures

    @property
    def estimated_bytes(self) -> int:
        return self.figures.estimated_bytes

    @property
    def free_bytes(self) -> int:
        return self.figures.free_bytes

    @property
    def reserve_bytes(self) -> int:
        return self.figures.reserve_bytes

    @property
    def usable_bytes(self) -> int:
        return self.figures.usable_bytes


def _natural(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} is a natural number of bytes")
    return value


def check_add_fits(estimated_bytes: int, *, free_bytes: int, reserve_bytes: int) -> SpaceFigures:
    """The figures when the addition fits, `AddSpaceRefusedError` when it does not. An addition
    that is estimated at nothing always fits."""
    figures = SpaceFigures(
        _natural(estimated_bytes, "estimated_bytes"),
        _natural(free_bytes, "free_bytes"),
        _natural(reserve_bytes, "reserve_bytes"),
    )
    if figures.estimated_bytes > figures.usable_bytes:
        raise AddSpaceRefusedError(figures)
    return figures
