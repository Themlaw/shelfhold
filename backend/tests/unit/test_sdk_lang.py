"""ENF-I18N-005: the SDK type is a plain str the host validates; plugins never normalize."""

import pytest
from shelfhold.sdk.lang import PLACEHOLDER_LANGUAGES, LanguageTag


@pytest.mark.req("ENF-I18N-005")
def test_language_tag_is_a_str_newtype_and_placeholders_are_declared() -> None:
    tag = LanguageTag("pt-BR")
    assert tag == "pt-BR"
    assert {"all", "multi", "und", "mul"} <= PLACEHOLDER_LANGUAGES
    assert "" in PLACEHOLDER_LANGUAGES
