"""EF-SRC-037: the order of the selection of a Unit's source: tombstones, filters, team, rank."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.acquisition.domain.selection import Candidate, select
from shelfhold.acquisition.domain.team_policy import TeamChoice

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def _c(variant: str, rank: int, team: str | None = "t", **flags: object) -> Candidate:
    return Candidate(
        variant_id=variant, binding_id=f"b{rank}", rank=rank, team_id=team,
        age=(NOW - timedelta(days=rank), variant), **flags,  # type: ignore[arg-type]
    )  # fmt: skip


def _any_team(teams: set[str | None]) -> TeamChoice:
    return TeamChoice(sorted(teams, key=lambda t: t or "")[0], "freshest")


@pytest.mark.req("EF-SRC-037")
def test_best_rank_that_has_the_unit_wins() -> None:
    picked = select([_c("v2", 2), _c("v1", 1), _c("v3", 3)], _any_team)
    assert picked is not None and picked.candidate.variant_id == "v1"
    only_second = select([_c("v2", 2), _c("v3", 3)], _any_team)
    assert only_second is not None and only_second.candidate.variant_id == "v2"


@pytest.mark.req("EF-SRC-037")
def test_team_preference_beats_rank() -> None:
    def prefer_b(teams: set[str | None]) -> TeamChoice:
        return TeamChoice("b", "priority")

    picked = select([_c("v1", 1, "a"), _c("v2", 2, "b")], prefer_b)
    assert picked is not None and picked.candidate.variant_id == "v2"


@pytest.mark.req("EF-SRC-037")
@pytest.mark.parametrize(
    "flag",
    [{"tombstoned": True}, {"active": False}, {"same_work": False}, {"filtered": "language"}],
)
def test_a_refused_candidate_is_never_selected_whatever_its_rank(flag: dict[str, object]) -> None:
    """Tombstones, inactive bindings, another Work's binding and a download filter all come
    before the team and the rank: the best-ranked one is out, the next is asked."""
    assert select([_c("v1", 1, **flag)], _any_team) is None
    picked = select([_c("v1", 1, **flag), _c("v2", 2)], _any_team)
    assert picked is not None and picked.candidate.variant_id == "v2"


@pytest.mark.req("EF-SRC-037")
def test_team_is_chosen_among_the_candidates_that_passed_the_filters() -> None:
    seen: list[set[str | None]] = []

    def spy(teams: set[str | None]) -> TeamChoice:
        seen.append(set(teams))
        return TeamChoice("b", "priority")

    select([_c("v1", 1, "a", tombstoned=True), _c("v2", 2, "b"), _c("v3", 3, "c")], spy)
    assert seen == [{"b", "c"}]  # a refused team never takes part in the election
