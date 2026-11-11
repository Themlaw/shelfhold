"""ENF-I18N-003 (Z3-c): the sort key drops the article of the language of the Edition."""

import pytest

from shelfhold.catalog.domain.sort_key import sort_key

pytestmark = pytest.mark.req("ENF-I18N-003")


def test_sort_without_article_uses_edition_language() -> None:
    assert sort_key("Le Petit Prince", "fr") == "petit prince"
    assert sort_key("L'Étranger", "fr") == "étranger"
    assert sort_key("The Hobbit", "en") == "hobbit"
    # the same letters are not an article in another language: the UI locale never decides
    assert sort_key("The Hobbit", "fr") == "the hobbit"
    assert sort_key("Le Petit Prince", "en") == "le petit prince"
    assert sort_key("Der Process", "de") == "process"
    assert sort_key("El Quijote", "es-419") == "quijote"


def test_sort_keeps_the_title_when_the_article_is_the_whole_title() -> None:
    assert sort_key("The", "en") == "the"
    assert sort_key("Les", "fr") == "les"


def test_sort_with_unknown_or_placeholder_language_strips_nothing() -> None:
    assert sort_key("The Hobbit", "x-unknown") == "the hobbit"
    assert sort_key("The Hobbit", "zh-Hant") == "the hobbit"
