"""Which Variant of a Unit to ask for, among the bindings of an Edition (EF-SRC-037).

The order is the spec's: tombstones, then the download filters (EF-SRC-039), then the team
(EF-BIB-035, whichever binding publishes it), then the best rank that has the Unit. A candidate a
step refuses never reaches the next one: a refused team takes no part in the election of the team.
Pure: the facts come from the caller.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime

from shelfhold.acquisition.domain.team_policy import TeamChoice, TeamKey

__all__ = ["Candidate", "Selection", "select"]


@dataclass(frozen=True)
class Candidate:
    variant_id: str
    binding_id: str
    rank: int  # `source_binding.priority`: 1 is the best
    team_id: TeamKey
    age: tuple[datetime, str]  # first seen, then id: the tie-break
    active: bool = True  # the binding is active (a fallback leaves it inactive)
    tombstoned: bool = False  # EF-BIB-053
    same_work: bool = True  # the binding belongs to the Edition's own Work
    filtered: str | None = None  # the reason a download filter refused it (EF-SRC-039)
    # Why the numbering check of EF-BIB-028 suspended a fallback to this offer (None: not
    # checked, or it passed). The verdict is the catalog's; selection only obeys it.
    offset_refusal: str | None = None


@dataclass(frozen=True)
class Selection:
    candidate: Candidate
    choice: TeamChoice


def select(
    candidates: Collection[Candidate], choose_team: Callable[[set[TeamKey]], TeamChoice]
) -> Selection | None:
    pool = [c for c in candidates if not c.tombstoned and c.filtered is None]  # tombstones, filters
    pool = [c for c in pool if c.offset_refusal is None]  # a fallback the numbering check suspended
    pool = [c for c in pool if c.active and c.same_work]  # a binding that can be asked at all
    if not pool:
        return None
    choice = choose_team({c.team_id for c in pool})
    chosen = min((c for c in pool if c.team_id == choice.team_id), key=lambda c: (c.rank, c.age))
    return Selection(chosen, choice)
