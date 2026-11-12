"""EF-BIB-028 (GAP-8-05): the numbering check held before a team fallback, and what `select`
does with its verdict. Every "suspended" here is an attack on the rule: it must hold."""

from datetime import UTC, datetime

import pytest

from shelfhold.acquisition.domain.selection import Candidate, select
from shelfhold.acquisition.domain.team_policy import TeamChoice
from shelfhold.catalog.domain.team_offset import (
    OFFSET_INCONSISTENT,
    OFFSET_UNEXPLAINED,
    OFFSET_UNVERIFIED,
    TeamFacts,
    check_before_fallback,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)


@pytest.mark.req("EF-BIB-028")
def test_a_gap_nothing_explains_suspends_the_fallback() -> None:
    """No offset known on either side: a different title or page count is unexplained."""
    held = TeamFacts(title="La Voie", page_count=20)
    assert check_before_fallback(held, TeamFacts(title="Autre", page_count=20)).reason == (
        OFFSET_UNEXPLAINED
    )
    assert check_before_fallback(held, TeamFacts(title="La Voie", page_count=19)).reason == (
        OFFSET_UNEXPLAINED
    )


@pytest.mark.req("EF-BIB-028")
def test_a_declared_offset_the_evidence_contradicts_is_inconsistent() -> None:
    held = TeamFacts(offset=0, title="La Voie", page_count=20)
    verdict = check_before_fallback(held, TeamFacts(offset=1, title="La Voie", page_count=31))
    assert not verdict.ok and verdict.reason == OFFSET_INCONSISTENT


@pytest.mark.req("EF-BIB-028")
def test_no_offset_known_and_nothing_to_compare_leaves_the_fallback_alone() -> None:
    assert check_before_fallback(TeamFacts(), TeamFacts()).ok
    assert check_before_fallback(TeamFacts(page_count=20), TeamFacts(title="x")).ok


@pytest.mark.req("EF-BIB-028")
def test_a_declared_offset_nothing_confirms_is_not_trusted() -> None:
    verdict = check_before_fallback(TeamFacts(), TeamFacts(offset=2))
    assert not verdict.ok and verdict.reason == OFFSET_UNVERIFIED
    # one side silent proves nothing either
    assert not check_before_fallback(TeamFacts(page_count=20), TeamFacts(offset=2)).ok


@pytest.mark.req("EF-BIB-028")
def test_a_declared_offset_the_evidence_confirms_lets_the_fallback_go() -> None:
    held = TeamFacts(offset=0, title="  LA   voie ", page_count=20)
    assert check_before_fallback(held, TeamFacts(offset=1, title="la voie", page_count=20)).ok


@pytest.mark.req("EF-BIB-028")
def test_a_blank_title_is_no_title() -> None:
    assert check_before_fallback(TeamFacts(title=" "), TeamFacts(title="La Voie")).ok


def _c(variant: str, rank: int, team: str, **flags: object) -> Candidate:
    return Candidate(
        variant_id=variant, binding_id=f"b{rank}", rank=rank, team_id=team,
        age=(NOW, variant), **flags,  # type: ignore[arg-type]
    )  # fmt: skip


def _prefer_b(teams: set[str | None]) -> TeamChoice:
    return TeamChoice("b" if "b" in teams else "a", "freshest", "majority_inactive")


@pytest.mark.req("EF-BIB-028")
def test_select_never_picks_an_offer_the_numbering_check_refused() -> None:
    refused = _c("v2", 1, "b", offset_refusal=OFFSET_UNEXPLAINED)
    picked = select([refused, _c("v1", 2, "a")], _prefer_b)
    assert picked is not None and picked.candidate.variant_id == "v1"
    assert select([refused], _prefer_b) is None  # nothing else: the Variant on disk stays
