"""The default team policy (EF-BIB-028): never change translation in the middle of a series.

Without an explicit priority list, among the teams that offer a chapter:

1. the team that already holds the majority of the files on disk (a unique maximum);
2. otherwise the one most recently active, then the most complete;
3. a team silent for more than N months (6 by default) is left out, unless every candidate is.

Departing from the majority team is a *fallback* and carries its reason, so the caller can log it.
This module is pure: the figures come from the caller.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_INACTIVE_MONTHS = 6
_DAYS_PER_MONTH = 30

TeamKey = str | None  # a release without a team is a candidate like another


@dataclass(frozen=True)
class TeamStats:
    on_disk: int = 0  # Units of the Edition whose file on disk comes from this team
    last_release_at: datetime | None = None
    offered: int = 0  # Units of the Edition this team offers (completeness)


@dataclass(frozen=True)
class TeamChoice:
    team_id: TeamKey
    reason: str  # priority | majority_on_disk | freshest | most_complete | only_candidate
    fallback: str | None = None  # majority_inactive | majority_absent: what is written to the log


def majority_team(stats: Mapping[TeamKey, TeamStats]) -> TeamKey:
    """The team with strictly the most files on disk; none when nothing is held or on a tie."""
    held = sorted(((s.on_disk, key) for key, s in stats.items() if s.on_disk > 0), key=_by_count)
    if not held or (len(held) > 1 and held[0][0] == held[1][0]):
        return None
    return held[0][1]


def _by_count(item: tuple[int, TeamKey]) -> tuple[int, str]:
    return (-item[0], item[1] or "")


def is_inactive(last_release_at: datetime | None, now: datetime, months: int) -> bool:
    if last_release_at is None:
        return False  # never seen releasing: unknown is not silent
    return now - last_release_at > timedelta(days=_DAYS_PER_MONTH * months)


def choose_team(
    candidates: Collection[TeamKey],
    stats: Mapping[TeamKey, TeamStats],
    *,
    priority: Sequence[str] = (),
    now: datetime,
    inactive_months: int = DEFAULT_INACTIVE_MONTHS,
) -> TeamChoice:
    ordered = sorted(candidates, key=lambda key: key or "")
    ranked = [key for key in priority if key in ordered]
    if ranked:
        return TeamChoice(ranked[0], "priority")  # the admin's own list: never a fallback
    if len(ordered) == 1 and majority_team(stats) in (None, ordered[0]):
        return TeamChoice(ordered[0], "only_candidate")

    def stat(key: TeamKey) -> TeamStats:
        return stats.get(key, TeamStats())

    pool = [
        key for key in ordered if not is_inactive(stat(key).last_release_at, now, inactive_months)
    ]
    pool = pool or ordered  # when every team is silent, none is left out
    majority = majority_team(stats)
    if majority is not None and majority in pool:
        return TeamChoice(majority, "majority_on_disk")
    fallback: str | None = None
    if majority is not None:
        fallback = "majority_inactive" if majority in ordered else "majority_absent"

    def freshness(key: TeamKey) -> float:
        last = stat(key).last_release_at
        return last.timestamp() if last is not None else float("-inf")

    best = max(freshness(key) for key in pool)
    freshest = [key for key in pool if freshness(key) == best]
    if len(freshest) == 1:
        return TeamChoice(freshest[0], "freshest", fallback)
    most = max(stat(key).offered for key in freshest)
    complete = [key for key in freshest if stat(key).offered == most]
    reason = "most_complete" if len(complete) < len(freshest) or len(freshest) > 1 else "freshest"
    return TeamChoice(complete[0], reason, fallback)
