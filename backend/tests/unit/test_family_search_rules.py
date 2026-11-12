"""M3 L19 (EF-USR-014, EF-USR-018): the pure rules of the family search: which form of the remote
search a reader gets (`family_search_mode`), and the subject and word filter (`is_flagged`)."""

from __future__ import annotations

import pytest

from shelfhold.catalog.domain.subject_filter import DEFAULT_ROOTS, clean_terms, is_flagged
from shelfhold.identity.domain.visibility import (
    ALL,
    NOBODY,
    ViewerScope,
    family_search_mode,
)


@pytest.mark.req("EF-USR-014", "EF-USR-018")
def test_family_search_mode_is_closed_by_default() -> None:
    assert family_search_mode(ALL, True) == "full"  # an adult: the federated search itself
    assert family_search_mode(ALL, False) == "full"
    assert family_search_mode(ViewerScope("ceiling", 6), False) == "reduced"
    assert family_search_mode(ViewerScope("ceiling", 0), False) == "reduced"
    assert family_search_mode(ViewerScope("ceiling", 6), True) == "hidden"
    assert family_search_mode(NOBODY, False) == "closed"  # unknown: not even reduced
    assert family_search_mode(NOBODY, True) == "closed"
    # anything that is not exactly `False` hides: a malformed setting never opens the door
    for odd in (None, 0, "false", "", [], 1):
        assert family_search_mode(ViewerScope("ceiling", 6), odd) == "hidden", odd  # type: ignore[arg-type]


@pytest.mark.req("EF-USR-018")
@pytest.mark.parametrize(
    "title",
    [
        "Erotica",
        "EROTICA",
        "érotica",
        "Éroticà of Spring",
        "Erotica!",
        "\uff25\uff32\uff2f\uff34\uff29\uff23\uff21",  # full-width letters
    ],
)
def test_default_terms_flag_by_word_whatever_the_case_accents_or_width(title: str) -> None:
    assert is_flagged(title, {}, ())


@pytest.mark.req("EF-USR-018")
def test_words_are_matched_whole_not_as_a_piece_of_another_word() -> None:
    assert not is_flagged("Essex Tales", {}, ())  # "sex" inside a word is not the word
    assert not is_flagged("The Adventure of the Dragon", {}, ())
    assert is_flagged("Sex education", {}, ())


@pytest.mark.req("EF-USR-018")
def test_subjects_of_the_result_are_read() -> None:
    assert is_flagged("A Quiet Tale", {"subjects": ["Fantasy", "Erotica -- History"]}, ())
    assert is_flagged("A Quiet Tale", {"subjects": "Erotica"}, ())
    assert not is_flagged("A Quiet Tale", {"subjects": ["Fantasy", "Animals"]}, ())
    assert not is_flagged("A Quiet Tale", {}, ())  # a source that says nothing: nothing to drop


@pytest.mark.req("EF-USR-018")
def test_subjects_that_cannot_be_read_are_flagged_not_ignored() -> None:
    for odd in (7, {"x": "y"}, [1, 2], ["ok", None], b"bytes", True):
        assert is_flagged("A Quiet Tale", {"subjects": odd}, ()), odd


@pytest.mark.req("EF-USR-018")
def test_the_profile_adds_terms_and_never_removes_the_default_ones() -> None:
    assert is_flagged("Dragons of Old", {}, ("dragons",))
    assert is_flagged(
        "Dragons of Old", {"subjects": ["Mythical Creatures"]}, ("mythical creatures",)
    )
    assert not is_flagged("Dragons of Old", {}, ())
    assert is_flagged(
        "Erotica", {}, ("dragons",)
    )  # the default list holds whatever the profile has
    assert "erot" in DEFAULT_ROOTS


@pytest.mark.req("EF-USR-018")
def test_terms_of_a_profile_are_cleaned_and_bounded() -> None:
    assert clean_terms(["  Dragons ", "dragons", "", "ÉROTIC"]) == ("dragons", "erotic")
    for odd in (None, "dragons", 3, {"a": 1}):
        assert clean_terms(odd) == ()
    assert clean_terms(["ok", 5, None, "fine"]) == ("ok", "fine")
    assert len(clean_terms([f"term{i}" for i in range(500)])) <= 100
    assert all(len(t) <= 64 for t in clean_terms(["x" * 1000]))
    # a term that normalises to nothing flags nothing (an empty term would flag everything)
    assert clean_terms(["--", "   ", "!!!"]) == ()
    assert not is_flagged("A Quiet Tale", {}, clean_terms(["--"]))
