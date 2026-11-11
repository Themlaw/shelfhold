"""The sort key of a title, without its leading article, by the language of the Edition.

ENF-I18N-003 (Z3-c): the article that is dropped is the one of the language the book is written
in, never the language of the interface: "The Hobbit" in an English Edition sorts under H even
when the atelier speaks French, and "Le Petit Prince" in an English Edition (a title kept in
French) is not stripped. The key is folded for comparison (NFC, casefold, one space).
"""

import unicodedata

__all__ = ["ARTICLES", "sort_key"]

# Primary language subtag -> articles (definite and indefinite). The elided forms end with an
# apostrophe and glue to the next word ("l'étranger").
ARTICLES: dict[str, tuple[str, ...]] = {
    "fr": ("le", "la", "les", "l'", "un", "une", "des"),
    "en": ("the", "a", "an"),
    "de": ("der", "die", "das"),  # the indefinite ones are also numerals: kept
    "es": ("el", "la", "los", "las", "un", "una"),
    "it": ("il", "lo", "la", "i", "gli", "le", "l'", "un", "una", "uno"),
    "pt": ("o", "a", "os", "as", "um", "uma"),
    "nl": ("de", "het", "een"),
}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\u2019", "'")  # typographic apostrophe
    return " ".join(text.casefold().split())


def sort_key(title: str, lang: str | None) -> str:
    """`title` folded for sorting, its leading article dropped for the language `lang` (a BCP 47
    tag). A title that is only an article keeps it; an unknown language strips nothing."""
    folded = _fold(title)
    primary = (lang or "").split("-", 1)[0].casefold()
    for article in ARTICLES.get(primary, ()):
        if article.endswith("'"):
            if folded.startswith(article) and len(folded) > len(article):
                return folded[len(article) :].lstrip()
        elif folded.startswith(article + " "):
            rest = folded[len(article) + 1 :].strip()
            if rest:
                return rest
    return folded
