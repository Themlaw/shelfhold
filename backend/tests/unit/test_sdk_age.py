"""EF-EXP-011: age stored as minimal integer age plus an X flag; frozen ComicInfo table."""

import pytest
from shelfhold.sdk.age import (
    AGE_TIERS,
    AgeRating,
    from_comicinfo,
    label_key,
    max_rating,
    to_columns,
    to_comicinfo,
)


@pytest.mark.req("EF-EXP-011")
@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        (AgeRating(0), "Everyone"),
        (AgeRating(10), "Everyone 10+"),
        (AgeRating(12), "Teen"),
        (AgeRating(13), "Teen"),
        (AgeRating(16), "Mature 17+"),
        (AgeRating(18), "Adults Only 18+"),
        (AgeRating(18, x=True), "X18+"),
        (None, None),
    ],
)
def test_comicinfo_forward_table_is_frozen(rating: AgeRating | None, expected: str | None) -> None:
    assert to_comicinfo(rating) == expected


@pytest.mark.req("EF-EXP-011")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Everyone", AgeRating(0)),
        ("G", AgeRating(0)),
        ("Kids to Adults", AgeRating(0)),
        ("Everyone 10+", AgeRating(10)),
        ("PG", AgeRating(10)),
        ("PEGI 12", AgeRating(12)),
        ("12+", AgeRating(12)),
        ("Teen", AgeRating(13)),
        ("MA15+", AgeRating(16)),
        ("Mature 17+", AgeRating(16)),
        ("M", AgeRating(16)),
        ("Adults Only 18+", AgeRating(18)),
        ("R18+", AgeRating(18)),
        ("X18+", AgeRating(18, x=True)),
        ("  teen ", AgeRating(13)),
        ("Unknown", None),
        ("Rating Pending", None),
        ("whatever", None),
    ],
)
def test_comicinfo_reverse_rule_gives_a_suggestion(value: str, expected: AgeRating | None) -> None:
    assert from_comicinfo(value) == expected


@pytest.mark.req("EF-EXP-011")
def test_age_stored_as_min_age_int_and_x_flag() -> None:
    assert to_columns(AgeRating(12)) == (12, False)
    assert to_columns(AgeRating(18, x=True)) == (18, True)
    assert to_columns(None) == (None, False)
    assert AGE_TIERS == (0, 10, 12, 13, 16, 18)
    assert to_comicinfo(AgeRating(12)) == "Teen"


@pytest.mark.req("EF-EXP-011")
def test_x_is_always_the_most_restrictive_and_comparison_is_arithmetic() -> None:
    assert max_rating([AgeRating(12), AgeRating(16)]) == AgeRating(16)
    assert max_rating([AgeRating(18), AgeRating(18, x=True), AgeRating(10)]) == AgeRating(
        18, x=True
    )
    assert max_rating([]) is None
    assert AgeRating(12) < AgeRating(13) < AgeRating(18) < AgeRating(18, x=True)
    assert AgeRating(16).allowed_under(AgeRating(16))
    assert not AgeRating(18, x=True).allowed_under(AgeRating(18))
    assert AgeRating(0).allowed_under(AgeRating(12))


@pytest.mark.req("EF-EXP-011")
def test_invalid_ratings_and_new_tier_without_migration() -> None:
    with pytest.raises(ValueError, match="age"):
        AgeRating(-1)
    with pytest.raises(ValueError, match="age"):
        AgeRating(7, x=True)  # X implies the highest tier
    assert to_comicinfo(AgeRating(14)) == "Mature 17+"  # next tier up in the frozen table
    assert label_key(AgeRating(12)) == "age.min.12"
    assert label_key(AgeRating(0)) == "age.all"
    assert label_key(AgeRating(18, x=True)) == "age.x"
    assert label_key(None) == "age.unrated"
