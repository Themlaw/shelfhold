"""ENF-I18N-005: normalize_lang is the single producer of BCP 47 tags (synthetic corpus)."""

import configparser
from pathlib import Path

import pytest

from shelfhold.catalog.lang import (
    LANG_NORMALIZER_ID,
    LanguageError,
    edition_key_lang,
    normalize_lang,
    variant_lang,
)

CASES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "lang_tags" / "cases.tsv"


def _cases() -> list[tuple[str, str, str]]:
    rows = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            raw, tag, key = line.split("\t")
            rows.append((raw, tag, key))
    return rows


def _tag(raw: str) -> str:
    try:
        return str(normalize_lang(raw).tag)
    except LanguageError as exc:
        return f"!{exc.code}"


def _key(raw: str) -> str:
    try:
        return str(edition_key_lang(raw))
    except LanguageError as exc:
        return f"!{exc.code}"


@pytest.mark.req("ENF-I18N-005")
@pytest.mark.parametrize(("raw", "tag", "key"), _cases())
def test_synthetic_corpus_normalizes_as_documented(raw: str, tag: str, key: str) -> None:
    assert _tag(raw) == tag
    assert _key(raw) == key


@pytest.mark.req("ENF-I18N-005")
def test_equivalent_language_tags_same_edition() -> None:
    groups = [
        ["pt-BR", "pt-br", "PT_BR", " pt-br "],
        ["fil", "tl", "TL"],
        ["he", "iw"],
        ["es-419", "es-la", "es_LA"],
        ["ja-Latn", "ja-ro"],
        ["zh-Hans-CN", "zh-CN", "zh-hans-cn", "zh_CN"],
        ["zh-Hans-SG", "zh_SG"],
        ["zh-Hant-TW", "zh-TW", "zh-hant-tw"],
        ["zh-Hant-HK", "zh-HK", "zh-hk"],
    ]
    for group in groups:
        assert len({edition_key_lang(raw) for raw in group}) == 1, group


@pytest.mark.req("ENF-I18N-005")
def test_zh_requires_script() -> None:
    with pytest.raises(LanguageError) as err:
        edition_key_lang("zh")
    assert err.value.code == "script_required"
    assert normalize_lang("zh").tag == "zh"  # storable elsewhere, never in an edition key
    assert edition_key_lang("zh-Hans") == "zh-Hans"
    assert edition_key_lang("zh-TW") == "zh-Hant-TW"
    assert edition_key_lang("zh-hk") == "zh-Hant-HK"
    assert edition_key_lang("zh-Hans-CN") == "zh-Hans-CN"
    assert edition_key_lang("zh-CN-x-foo") == "zh-Hans-CN-x-foo"  # extensions kept, as elsewhere
    assert edition_key_lang("zh-TW") != edition_key_lang("zh-HK")  # regions stay distinct


@pytest.mark.req("ENF-I18N-005", "EF-BIB-001")
@pytest.mark.parametrize("placeholder", ["all", "multi", "und", "mul", "ALL", ""])
def test_multi_language_extension_never_enters_edition_key(placeholder: str) -> None:
    with pytest.raises(LanguageError):
        edition_key_lang(placeholder)
    with pytest.raises(LanguageError):
        normalize_lang(placeholder)
    # a multi-language source takes the chapter's language, or the unit waits (language_unknown)
    assert variant_lang(placeholder, "fr-fr") == "fr-FR"
    assert variant_lang(placeholder, None) is None
    assert variant_lang(placeholder, "multi") is None
    assert variant_lang("en", None) == "en"
    assert variant_lang("en", "ja") == "ja"


@pytest.mark.req("ENF-I18N-005", "ENF-OPS-021")
def test_normalized_language_keeps_raw_and_normalizer_version() -> None:
    result = normalize_lang("PT_br")
    assert (result.tag, result.raw, result.by) == ("pt-BR", "PT_br", LANG_NORMALIZER_ID)
    assert LANG_NORMALIZER_ID == "lang@1"


@pytest.mark.req("ENF-I18N-005")
def test_lang_single_producer_contract_is_enforced_with_default_alerting() -> None:
    config = Path(__file__).resolve().parents[2] / "importlinter.ini"
    parser = configparser.ConfigParser()
    parser.read(config)
    section = parser["importlinter:contract:lang-single-producer"]
    assert "unmatched_ignore_imports_alerting" not in section  # default: error
    assert "shelfhold.catalog.lang -> langcodes" in section["ignore_imports"]
