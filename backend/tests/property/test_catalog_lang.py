"""ENF-I18N-005 properties: canonical tags are idempotent and case/separator invariant."""

import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.catalog.lang import LanguageError, normalize_lang

TAGS = st.sampled_from(
    [
        "fr",
        "en-US",
        "pt-BR",
        "zh-Hans",
        "zh-Hant",
        "zh-Hans-CN",
        "zh-Hant-TW",
        "zh-Hant-HK",
        "zh-CN",
        "zh-TW",
        "zh-HK",
        "sr-Latn-RS",
        "es-419",
        "ja",
        "tl",
        "iw",
        "fil",
    ]
)


@st.composite
def mangled(draw: st.DrawFn) -> tuple[str, str]:
    tag = draw(TAGS)
    out = "".join(c.upper() if draw(st.booleans()) else c.lower() for c in tag)
    return tag, out.replace("-", draw(st.sampled_from(["-", "_"])))


@pytest.mark.req("ENF-I18N-005")
@given(mangled())
def test_equivalent_language_tags_same_edition_property(pair: tuple[str, str]) -> None:
    clean, noisy = pair
    assert normalize_lang(noisy).tag == normalize_lang(clean).tag


@pytest.mark.req("ENF-I18N-005")
@given(TAGS)
def test_normalize_lang_is_idempotent(tag: str) -> None:
    once = normalize_lang(tag).tag
    assert normalize_lang(once).tag == once


@pytest.mark.req("ENF-I18N-005")
@given(st.text(max_size=20))
def test_normalize_lang_only_raises_language_error(raw: str) -> None:
    with contextlib.suppress(LanguageError):
        normalize_lang(raw)


@pytest.mark.req("ENF-I18N-005")
@given(st.sampled_from(["zh-CN", "zh-SG", "zh-TW", "zh-HK", "zh-MO", "zh-Hans", "zh-Hant"]))
def test_chinese_edition_keys_always_carry_a_script(tag: str) -> None:
    from shelfhold.catalog.lang import edition_key_lang

    key = edition_key_lang(tag)
    assert key.split("-")[1] in {"Hans", "Hant"}
    assert edition_key_lang(key) == key
