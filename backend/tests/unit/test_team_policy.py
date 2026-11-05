"""EF-BIB-028: the default team policy (majority on disk, then freshest, inactive teams out)."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.acquisition.domain.team_policy import (
    TeamStats,
    choose_team,
    majority_team,
)

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def _stats(on_disk: int = 0, days_ago: float | None = 1.0, offered: int = 0) -> TeamStats:
    last = None if days_ago is None else NOW - timedelta(days=days_ago)
    return TeamStats(on_disk=on_disk, last_release_at=last, offered=offered)


@pytest.mark.req("EF-BIB-028")
def test_team_default_majority_on_disk_then_freshest_excluding_inactive() -> None:
    # two teams offer "Ch.10"; B already holds 9 chapters on disk: B, never a change mid-series
    stats = {"A": _stats(0, 1), "B": _stats(9, 20)}
    kept = choose_team(["A", "B"], stats, now=NOW)
    assert kept.team_id == "B" and kept.reason == "majority_on_disk" and kept.fallback is None

    # B silent for more than six months: the fallback to A is explicit
    stale = {"A": _stats(0, 1), "B": _stats(9, 200)}
    fell = choose_team(["A", "B"], stale, now=NOW)
    assert fell.team_id == "A" and fell.fallback == "majority_inactive"
    # N is adjustable: with a year, B stays
    assert choose_team(["A", "B"], stale, now=NOW, inactive_months=12).team_id == "B"

    # nothing on disk yet: the most recently active, then the most complete
    fresh = choose_team(["A", "B"], {"A": _stats(0, 30), "B": _stats(0, 3)}, now=NOW)
    assert fresh.team_id == "B" and fresh.reason == "freshest" and fresh.fallback is None
    tie = choose_team(
        ["A", "B"], {"A": _stats(0, 3, offered=40), "B": _stats(0, 3, offered=90)}, now=NOW
    )
    assert tie.team_id == "B" and tie.reason == "most_complete"

    # the majority team does not offer this chapter: another one, and the departure is said
    absent = choose_team(["A"], {"A": _stats(0, 1), "B": _stats(9, 1)}, now=NOW)
    assert absent.team_id == "A" and absent.fallback == "majority_absent"

    # when every candidate is inactive, none is excluded: a chapter is never left without team
    assert choose_team(["A", "B"], {"A": _stats(0, 400), "B": _stats(0, 300)}, now=NOW).team_id


@pytest.mark.req("EF-BIB-028")
def test_explicit_priority_list_wins_over_the_default_rule() -> None:
    stats = {"A": _stats(0, 1), "B": _stats(9, 1)}
    chosen = choose_team(["A", "B"], stats, priority=["A", "B"], now=NOW)
    assert chosen.team_id == "A" and chosen.reason == "priority" and chosen.fallback is None


@pytest.mark.req("EF-BIB-028")
def test_a_release_without_team_is_a_candidate_like_any_other() -> None:
    assert choose_team([None], {}, now=NOW).team_id is None
    assert majority_team({"A": _stats(2), "B": _stats(2)}) is None  # no unique majority


TEAMS = st.sampled_from(["A", "B", "C", "D"])


@pytest.mark.req("EF-BIB-028")
@given(
    stats=st.dictionaries(
        TEAMS,
        st.builds(
            TeamStats,
            on_disk=st.integers(0, 20),
            last_release_at=st.one_of(
                st.none(), st.integers(0, 600).map(lambda d: NOW - timedelta(days=d))
            ),
            offered=st.integers(0, 50),
        ),
    ),
    candidates=st.sets(TEAMS, min_size=1),
)
def test_never_switch_team_mid_series_without_log(
    stats: dict[str | None, TeamStats], candidates: set[str]
) -> None:
    """Whenever the team chosen is not the one holding the majority of the files, the choice
    carries the reason that Shelfhold writes to the log (`fallback`)."""
    choice = choose_team(sorted(candidates), stats, now=NOW)
    assert choice.team_id in candidates
    majority = majority_team(stats)
    if majority is not None and choice.team_id != majority:
        assert choice.fallback in {"majority_inactive", "majority_absent"}
    else:
        assert choice.fallback is None
