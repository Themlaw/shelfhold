"""`normalize_lang()`: the only producer of language tags (ENF-I18N-005, ENF-OPS-021).

Canonical BCP 47 tag (RFC 5646): lowercase language, title-case script, uppercase region. The
received value is kept next to it (`raw`) with the normalizer id `lang@1`. The import-linter
contract `lang-single-producer` reserves `langcodes` for this module; plugins do not normalize (the
SDK only carries the `LanguageTag` type).

Version 1 choice: for Chinese only the script matters in the key (`zh-Hans`, `zh-Hant`), the region
is dropped; a bare `zh` is a valid tag but never enters an Edition key (`edition_key_lang`). Any
addition to the alias table is a new version.
"""

from dataclasses import dataclass

import langcodes

from shelfhold.sdk.lang import PLACEHOLDER_LANGUAGES, LanguageTag

LANG_NORMALIZER_NAME = "lang"
LANG_NORMALIZER_VERSION = 1
LANG_NORMALIZER_ID = f"{LANG_NORMALIZER_NAME}@{LANG_NORMALIZER_VERSION}"

# Aliases applied before langcodes: obsolete or source-specific codes, and romanizations (`ja-ro`
# is Japanese in Latin script). Lowercase keys, `-` separator.
_ALIASES: dict[str, str] = {
    "tl": "fil",
    "iw": "he",
    "in": "id",
    "ji": "yi",
    "jw": "jv",
    "mo": "ro",
    "jp": "ja",
    "es-la": "es-419",
    "ja-ro": "ja-Latn",
    "ko-ro": "ko-Latn",
    "zh-ro": "zh-Latn",
}
_ZH_HANT_REGIONS = frozenset({"TW", "HK", "MO"})


class LanguageError(ValueError):
    """Refused tag; `code` is one of `placeholder`, `invalid`, `script_required`."""

    def __init__(self, code: str, raw: str) -> None:
        super().__init__(f"{code}: {raw!r}")
        self.code = code
        self.raw = raw


@dataclass(frozen=True)
class NormalizedLang:
    tag: LanguageTag
    raw: str
    by: str = LANG_NORMALIZER_ID


def _canonical(raw: str) -> str:
    cleaned = raw.strip().replace("_", "-")
    if cleaned.casefold() in PLACEHOLDER_LANGUAGES:
        raise LanguageError("placeholder", raw)
    lowered = cleaned.casefold()
    head, _, rest = lowered.partition("-")
    cleaned = _ALIASES.get(lowered) or (
        f"{_ALIASES[head]}-{rest}" if rest and head in _ALIASES else lowered
    )
    # language-data (CLDR tables) is optional in langcodes: standardize_tag works without it.
    if not langcodes.tag_is_valid(cleaned):
        raise LanguageError("invalid", raw)
    try:
        tag = langcodes.standardize_tag(cleaned)
    except (ValueError, langcodes.tag_parser.LanguageTagError) as exc:
        raise LanguageError("invalid", raw) from exc
    language = langcodes.Language.get(tag)
    if language.language == "zh":
        script = language.script or (
            "Hant"
            if language.territory in _ZH_HANT_REGIONS
            else "Hans"
            if language.territory
            else ""
        )
        return f"zh-{script}" if script else "zh"
    return tag


def normalize_lang(raw: str) -> NormalizedLang:
    """Canonical tag of `raw`; `LanguageError` for a placeholder or an invalid tag."""
    return NormalizedLang(LanguageTag(_canonical(raw)), raw)


def edition_key_lang(raw: str) -> LanguageTag:
    """Language allowed in an Edition key (no placeholder, no `zh` without a script)."""
    tag = normalize_lang(raw).tag
    if tag == "zh":
        raise LanguageError("script_required", raw)
    return tag


def variant_lang(source_lang: str | None, chapter_lang: str | None) -> LanguageTag | None:
    """Language of a Variant: the chapter's if the source gives it, else the source's.

    `None` means unknown language: the Unit goes to `waiting_state = language_unknown`.
    """
    for candidate in (chapter_lang, source_lang):
        if candidate is None:
            continue
        try:
            return normalize_lang(candidate).tag
        except LanguageError:
            continue
    return None
