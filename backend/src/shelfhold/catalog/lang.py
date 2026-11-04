"""`normalize_lang()` : seul producteur d'étiquettes de langue (ENF-I18N-005, ENF-OPS-021).

Étiquette BCP 47 canonique (RFC 5646) : langue en minuscules, écriture en casse de titre, région
en majuscules. La valeur reçue est gardée à côté (`raw`) avec l'identifiant `lang@1` du normaliseur.
Le contrat import-linter `lang-single-producer` réserve `langcodes` à ce module ; les plugins ne
normalisent pas (le SDK ne porte que le type `LanguageTag`).

Choix de la version 1 : pour le chinois, seule l'écriture compte dans la clé (`zh-Hans`,
`zh-Hant`), la région est abandonnée ; `zh` nu reste une étiquette valide mais n'entre jamais dans
une clé d'Édition (`edition_key_lang`). Les ajouts à la table d'alias sont une nouvelle version.
"""

from dataclasses import dataclass

import langcodes

from shelfhold.sdk.lang import PLACEHOLDER_LANGUAGES, LanguageTag

LANG_NORMALIZER_NAME = "lang"
LANG_NORMALIZER_VERSION = 1
LANG_NORMALIZER_ID = f"{LANG_NORMALIZER_NAME}@{LANG_NORMALIZER_VERSION}"

# Alias appliqués avant langcodes : codes obsolètes ou usuels chez les sources, et romanisations
# (`ja-ro` désigne du japonais en écriture latine). Clés en minuscules, séparateur `-`.
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
    """Étiquette refusée ; `code` ∈ {`placeholder`, `invalid`, `script_required`}."""

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
    """Étiquette canonique de `raw` ; `LanguageError` si c'est un joker ou si elle est invalide."""
    return NormalizedLang(LanguageTag(_canonical(raw)), raw)


def edition_key_lang(raw: str) -> LanguageTag:
    """Langue admise dans la clé d'Édition (ni joker, ni `zh` sans écriture)."""
    tag = normalize_lang(raw).tag
    if tag == "zh":
        raise LanguageError("script_required", raw)
    return tag


def variant_lang(source_lang: str | None, chapter_lang: str | None) -> LanguageTag | None:
    """Langue d'une Variante : celle du chapitre si la source la donne, sinon celle de la source.

    `None` : langue inconnue, l'Unité passe en `waiting_state = language_unknown`.
    """
    for candidate in (chapter_lang, source_lang):
        if candidate is None:
            continue
        try:
            return normalize_lang(candidate).tag
        except LanguageError:
            continue
    return None
