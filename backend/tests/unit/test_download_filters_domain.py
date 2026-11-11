"""EF-SRC-039 and EF-BIB-056: the pure rules of the filters and of the gaps."""

from decimal import Decimal

import pytest

from shelfhold.acquisition.domain.filters import UnitFacts, parse_filters, refusal
from shelfhold.acquisition.domain.gaps import MAX_GAPS, Place, find_gaps
from shelfhold.acquisition.domain.watermark import Watermark


def _facts(**over: object) -> UnitFacts:
    base: dict[str, object] = {"lang": "fr", "team": "Team A", "track": "main", "segment": ""}
    return UnitFacts(**{**base, **over})  # type: ignore[arg-type]


@pytest.mark.req("EF-SRC-039")
def test_language_filter_applied_before_request_rules() -> None:
    filters = parse_filters({"languages": ["fr", "pt-BR"]})
    assert refusal(filters, _facts(lang="fr")) is None
    assert refusal(filters, _facts(lang="fr-CA")) is None
    assert refusal(filters, _facts(lang="pt-br")) is None
    for lang in ("en", "frr", "fra", "und", "pt"):
        assert refusal(filters, _facts(lang=lang)) == "language", lang


@pytest.mark.req("EF-SRC-039")
def test_extra_chapter_filtered_with_reason_rules() -> None:
    filters = parse_filters({"exclude_extras": True})
    assert refusal(filters, _facts()) is None
    for track in ("extra", "special", "unnumbered"):
        assert refusal(filters, _facts(track=track)) == "extra"
    assert refusal(filters, _facts(segment="S2")) == "extra"  # outside the main segment
    assert refusal(parse_filters({}), _facts(track="extra")) is None


@pytest.mark.req("EF-SRC-039")
def test_team_filters_rules() -> None:
    assert refusal(parse_filters({"teams_refuse": ["team a"]}), _facts()) == "team"
    assert refusal(parse_filters({"teams_refuse": ["team a"]}), _facts(team=None)) is None
    accepting = parse_filters({"teams_accept": ["Team B"]})
    assert refusal(accepting, _facts()) == "team"
    assert refusal(accepting, _facts(team=None)) == "team"
    assert refusal(accepting, _facts(team="TEAM B")) is None


@pytest.mark.req("EF-SRC-039")
@pytest.mark.parametrize(
    "bad",
    [[], "fr", {"languages": "fr"}, {"languages": [""]}, {"languages": ["a b"]},
     {"teams_accept": [None]}, {"exclude_extras": 1}, {"cap": True}, {"cap": -3}, {"x": 1}],
)  # fmt: skip
def test_unreadable_filters_refuse_everything(bad: object) -> None:
    filters = parse_filters(bad)
    assert filters.invalid and refusal(filters, _facts()) == "invalid"


@pytest.mark.req("EF-SRC-039")
def test_no_filters_means_open() -> None:
    assert parse_filters(None).is_open and parse_filters({"news_only": True, "cap": 5}).cap == 5
    assert refusal(parse_filters(None), _facts(lang="und", team=None)) is None


def _places(*numbers: str, track: str = "main", segment: str = "", ordinal: int = 0) -> list[Place]:
    return [Place(track, segment, ordinal, Decimal(n)) for n in numbers]


@pytest.mark.req("EF-BIB-056")
def test_gaps_rules() -> None:
    mark = Watermark.of("", "0")
    assert [g.number for g in find_gaps(_places("10", "12"), mark, frozenset())] == [11]
    assert find_gaps(_places("10", "10.5", "11"), mark, frozenset()) == []
    assert [g.number for g in find_gaps(_places("10.5", "12"), mark, frozenset())] == [11]
    assert find_gaps(_places("10", "11"), None, frozenset()) == []  # no watermark: no "beyond"
    assert find_gaps(_places("10", "12"), mark, frozenset({("main", "", "11")})) == []


@pytest.mark.req("EF-BIB-056")
def test_gaps_only_beyond_the_watermark_and_per_run() -> None:
    mark = Watermark.of("", "10")
    assert [g.number for g in find_gaps(_places("8", "13"), mark, frozenset())] == [11, 12]
    assert find_gaps(_places("1", "5"), mark, frozenset()) == []
    # another track or segment is another run: 1 and 3 of S2 leave 2 of S2, not of the main one
    runs = _places("10", "11") + _places("1", "3", segment="S2", ordinal=2)
    assert [(g.segment, g.number) for g in find_gaps(runs, mark, frozenset())] == [("S2", 2)]
    older = Watermark.of("S2", "1")
    assert find_gaps(_places("1", "9", segment="S1", ordinal=1), older, frozenset()) == []


@pytest.mark.req("EF-BIB-056")
def test_gaps_are_bounded() -> None:
    found = find_gaps(_places("1", "99999999"), Watermark.of("", "0"), frozenset())
    assert len(found) == MAX_GAPS
