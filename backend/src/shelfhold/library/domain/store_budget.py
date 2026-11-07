"""The budget of /store: what a batch of kept copies may take (EF-BIB-042 (4), EF-BIB-027).

One rule, shared by the catch-up of a shelf switched to "keep a copy" (this lot) and by "Mettre à
l'abri" (L16): before anything is queued, the estimated size must fit in the free space of /store
minus the reserve of ENF-OPS-006 (the disk guard's pause mark); work goes by batches capped at 200
Units or 5 GiB per cycle. What does not fit stays queued as a backlog, reported once
(`unit.safeguard_backlog`), and the oldest Units come first so a later cycle resumes in order.
Plain data: it measures nothing and touches no disk.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

GIB: Final = 1 << 30
DEFAULT_BATCH_UNITS: Final = 200
DEFAULT_BATCH_BYTES: Final = 5 * GIB


@dataclass(frozen=True)
class Candidate:
    unit_id: str
    size: int  # estimated bytes the copy will take in /store


@dataclass(frozen=True)
class StoreBudget:
    free_bytes: int  # free space of the volume that holds /store
    reserve_bytes: int  # what the disk guard keeps (ENF-OPS-006)
    max_units: int = DEFAULT_BATCH_UNITS
    max_bytes: int = DEFAULT_BATCH_BYTES

    @property
    def room(self) -> int:
        """What may still be queued: free space above the reserve, never negative."""
        return max(0, self.free_bytes - self.reserve_bytes)


@dataclass(frozen=True)
class Batch:
    selected: tuple[Candidate, ...]
    deferred: tuple[Candidate, ...]

    @property
    def backlog(self) -> int:
        """Units left for a later cycle: `unit.safeguard_backlog` when above zero."""
        return len(self.deferred)

    @property
    def bytes(self) -> int:
        return sum(c.size for c in self.selected)


def plan_batch(candidates: Sequence[Candidate], budget: StoreBudget) -> Batch:
    """The longest prefix of `candidates` that stays under the caps and fits in the room.

    A Unit larger than the per-cycle byte cap goes alone rather than never (it would block the
    queue for ever), but still only when it fits in the room."""
    room = min(budget.room, budget.max_bytes)
    taken = 0
    count = 0
    for candidate in candidates:
        if count >= budget.max_units:
            break
        oversize = candidate.size > budget.max_bytes and count == 0
        fits = taken + candidate.size <= room or (oversize and candidate.size <= budget.room)
        if not fits:
            break
        taken += candidate.size
        count += 1
        if oversize:
            break
    return Batch(tuple(candidates[:count]), tuple(candidates[count:]))
