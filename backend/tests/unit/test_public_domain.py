"""EF-SRC-024, EF-BIB-046 (ADR-0032): the indicative public-domain calculation, in pure code."""

from datetime import date

import pytest

from shelfhold.catalog.domain.public_domain import (
    Holder,
    Verdict,
    assess,
    protected_until,
)

TODAY = date(2026, 11, 11)


@pytest.mark.req("EF-SRC-024")
def test_france_died_for_france_stays_protected_and_extensions_add_up() -> None:
    plain = protected_until("FR", death_year=1944, first_published_year=1939, died_for_france=False)
    mpf = protected_until("FR", death_year=1944, first_published_year=1939, died_for_france=True)
    assert plain is not None and mpf is not None
    assert plain > date(2014, 12, 31)  # life + 70 plus the war extensions
    assert mpf.year == plain.year + 30


@pytest.mark.req("EF-SRC-024")
def test_france_old_author_is_clear() -> None:
    until = protected_until("FR", death_year=1885, first_published_year=1862, died_for_france=False)
    assert until is not None and until < TODAY


@pytest.mark.req("EF-SRC-024")
def test_us_rule_counts_from_first_publication_not_from_death() -> None:
    old = assess("US", [Holder("aut", 2001)], first_published_year=1925, today=TODAY)
    assert old.verdict is Verdict.NO_WARNING
    recent = assess("US", [Holder("aut", 1900)], first_published_year=1945, today=TODAY)
    assert recent.verdict is Verdict.MAY_BE_PROTECTED


@pytest.mark.req("EF-SRC-024")
def test_licence_and_user_supplied_never_warn() -> None:
    holders = [Holder("aut", 2020)]
    for basis in ("licence", "user_supplied", "worldwide_public_domain"):
        result = assess("FR", holders, first_published_year=2000, today=TODAY, rights_basis=basis)
        assert result.verdict is Verdict.NO_WARNING
        assert result.warnings == ()


@pytest.mark.req("EF-SRC-024")
def test_missing_death_dates_say_no_information_never_clear() -> None:
    result = assess("FR", [Holder("aut", None)], first_published_year=1900, today=TODAY)
    assert result.verdict is Verdict.NO_INFORMATION
    assert (
        assess("FR", [], first_published_year=1900, today=TODAY).verdict is Verdict.NO_INFORMATION
    )


@pytest.mark.req("EF-BIB-046")
def test_the_latest_holder_wins_over_the_author() -> None:
    holders = [Holder("aut", 1936, contributor_id="A"), Holder("ill", 1976, contributor_id="B")]
    result = assess("FR", holders, first_published_year=1930, today=TODAY)
    assert result.verdict is Verdict.MAY_BE_PROTECTED
    assert [w.role for w in result.warnings] == ["ill"]


@pytest.mark.req("EF-SRC-024")
@pytest.mark.parametrize(
    ("year", "today"),
    [(1914, date(2015, 6, 1)), (1916, date(2015, 6, 1)), (1918, date(2015, 6, 1)),
     (1939, TODAY), (1944, TODAY), (1945, TODAY)],
)  # fmt: skip
def test_france_unknown_died_for_france_warns_when_the_extension_could_apply(
    year: int, today: date
) -> None:
    """M2 L15b: unknown (None) is read on the cautious side, only where the law could apply: the
    end of the term counts the 30 years, so the warning goes on while they could still run."""
    unknown = assess("FR", [Holder("aut", year, None)], first_published_year=year, today=today)
    yes = assess("FR", [Holder("aut", year, True)], first_published_year=year, today=today)
    assert unknown.verdict == yes.verdict == Verdict.MAY_BE_PROTECTED, year
    assert unknown.warnings[0].protected_until == yes.warnings[0].protected_until
    assert protected_until(
        "FR", death_year=year, first_published_year=year, died_for_france=False
    ) < unknown.warnings[0].protected_until


@pytest.mark.req("EF-SRC-024")
def test_france_unknown_died_for_france_stops_warning_once_even_the_30_years_are_over() -> None:
    late = assess("FR", [Holder("aut", 1916, None)], first_published_year=1910, today=TODAY)
    assert late.verdict == Verdict.NO_WARNING  # 1916: the 30 years ended in 2022 at the latest


@pytest.mark.req("EF-SRC-024")
@pytest.mark.parametrize("year", [1913, 1919, 1938, 1946, 1960])
def test_france_unknown_died_for_france_outside_the_wars_adds_nothing(year: int) -> None:
    """Attack: the cautious reading must not stretch the term of an author who died in peace."""
    unknown = protected_until("FR", death_year=year, first_published_year=year, died_for_france=None)
    no = protected_until("FR", death_year=year, first_published_year=year, died_for_france=False)
    assert unknown == no


@pytest.mark.req("EF-SRC-024")
def test_unknown_died_for_france_never_warns_outside_france_and_never_blocks() -> None:
    clear = assess("DE", [Holder("aut", 1916, None)], first_published_year=1910, today=TODAY)
    assert clear.verdict == Verdict.NO_WARNING  # life + 70 only, no French extension elsewhere
    assert protected_until("US", death_year=1916, first_published_year=1910, died_for_france=None) is None
