"""Which Variant of a Unit to ask for, among the bindings of an Edition (EF-SRC-037).

The order is the spec's: tombstones, then the download filters (EF-SRC-039), then the team
(EF-BIB-035, whichever binding publishes it), then the best rank that has the Unit. A candidate a
step refuses never reaches the next one: a refused team takes no part in the election of the team.
Pure: the facts come from the caller.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime

from shelfhold.acquisition.domain.team_offset import check as _check_team_offset
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
    # --- team numbering offset (EF-BIB-028, GAP-8-05) ------------------------------
    normalized_title: str | None = None
    page_count: int | None = None
    number_offset: int = 0
    team_offset: int = 0
    title: str | None = None
    pages: int | None = None
    offset_ok: bool = True


@dataclass(frozen=True)
class Selection:
    candidate: Candidate
    choice: TeamChoice


def _candidate_title(c: Candidate) -> str | None:
    return c.normalized_title if c.normalized_title is not None else c.title


def _candidate_pages(c: Candidate) -> int | None:
    return c.page_count if c.page_count is not None else c.pages


def _offset_filter(pool: list[Candidate]) -> list[Candidate]:
    # An explicit refusal always wins.
    pool = [c for c in pool if c.offset_ok]
    # Group by (normalized title, page count); a minority group whose title or pages disagree
    # with the majority is suspended (the fallback opens a suggestion instead).
    groups: dict[tuple[object, object], list[Candidate]] = {}
    for c in pool:
        title = _candidate_title(c)
        pages = _candidate_pages(c)
        if title is None and pages is None:
            continue
        groups.setdefault((title, pages), []).append(c)
    if len(groups) <= 1:
        return pool
    largest = max(len(v) for v in groups.values())
    majority_keys = {k for k, v in groups.items() if len(v) == largest}
    kept: list[Candidate] = []
    for c in pool:
        title = _candidate_title(c)
        pages = _candidate_pages(c)
        if title is None and pages is None:
            kept.append(c)  # no data to judge: keep
            continue
        if (title, pages) in majority_keys:
            kept.append(c)
        else:
            # only suspend when the offset actually disagrees (title or pages differ)
            check = _check_team_offset(
                normalized_title=title,
                other_normalized_title=next(iter(majority_keys))[0],
                page_count=pages,
                other_page_count=next(iter(majority_keys))[1],
            )
            if check.ok:
                kept.append(c)
    return kept or pool


def select(
    candidates: Collection[Candidate], choose_team: Callable[[set[TeamKey]], TeamChoice]
) -> Selection | None:
    pool = [c for c in candidates if not c.tombstoned and c.filtered is None]  # tombstones, filters
    pool = [c for c in pool if c.active and c.same_work]  # a binding that can be asked at all
    pool = _offset_filter(pool)
    if not pool:
        return None
    choice = choose_team({c.team_id for c in pool})
    chosen = min((c for c in pool if c.team_id == choice.team_id), key=lambda c: (c.rank, c.age))
    return Selection(chosen, choice)
