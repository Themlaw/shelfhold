"""EF-EXP-021 (M6 L16): the names of the Kobo folder — pure functions."""

from __future__ import annotations

import pytest

from shelfhold.export.targets.kobo.names import book_filename, series_folder


@pytest.mark.req("EF-EXP-021")
def test_book_filename_without_a_number_is_its_title() -> None:
    assert book_filename(None, "Dawn") == "Dawn.kepub.epub"
    assert book_filename("", "Dawn") == "Dawn.kepub.epub"


@pytest.mark.req("EF-EXP-021")
def test_book_filename_refuses_a_non_decimal_number() -> None:
    assert book_filename("abc", "Dawn").startswith("Ch.") is False
    assert book_filename("abc", "Dawn") == "Dawn.kepub.epub"


@pytest.mark.req("EF-EXP-021")
def test_book_filename_refuses_a_non_finite_or_negative_number() -> None:
    assert book_filename("nan", "Dawn") == "Dawn.kepub.epub"
    assert book_filename("-3", "Dawn") == "Dawn.kepub.epub"


@pytest.mark.req("EF-EXP-021")
def test_book_filename_drops_a_title_that_sanitizes_to_nothing() -> None:
    # a title made only of separators sanitizes to "_" then to ""
    assert book_filename("1", "   ...   ") == "Ch.0001.kepub.epub"


@pytest.mark.req("EF-EXP-021")
def test_series_folder_is_sanitized() -> None:
    assert series_folder("A/B") != "A/B"
