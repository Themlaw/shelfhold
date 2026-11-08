"""L11 (EF-INT-014, EF-EXP-048): the age typed on the command line, and the token of the batch."""

import pytest

from shelfhold.catalog.public import parse_age
from shelfhold.export.application.to_rate import ToRate, batch_token
from shelfhold.sdk.age import AgeRating

pytestmark = pytest.mark.req("EF-INT-014")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("13+", AgeRating(13)), ("13", AgeRating(13)), (" 0 ", AgeRating(0)),
        ("18+", AgeRating(18)), ("x", AgeRating(18, x=True)), ("X", AgeRating(18, x=True)),
    ],
)  # fmt: skip
def test_parse_age(text: str, expected: AgeRating) -> None:
    assert parse_age(text) == expected


@pytest.mark.parametrize("text", ["", "+", "treize", "-3", "1.5", "\uff11\uff13", "13++"])
def test_parse_age_refuses_what_is_not_an_age(text: str) -> None:
    with pytest.raises(ValueError, match="not an age"):
        parse_age(text)


def _items(*ids: str) -> list[ToRate]:
    return [ToRate(i, i, "rating_pending", "") for i in ids]


def test_batch_token_names_the_count_the_age_and_the_works() -> None:
    token = batch_token(_items("A", "B", "C"), "13+")
    assert token.startswith("3-")
    assert token == batch_token(_items("C", "B", "A"), "13+")  # the order is not part of it
    assert token != batch_token(_items("A", "B", "C"), "16+")  # nor is another age
    assert token != batch_token(_items("A", "B", "D"), "13+")  # another Work, same count
    assert token != batch_token(_items("A", "B"), "13+")
