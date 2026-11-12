"""What a person typed, as words (EF-BIB-010).

The query is never FTS5 syntax: it is cut in words, and each word becomes one quoted phrase (the
quotes inside it doubled), restricted to the two columns meant. `OR`, `NEAR`, `-`, `*`, `:`,
parentheses and column names are therefore text. A word with no letter or digit is dropped.
"""

import re
import unicodedata

__all__ = ["MAX_TERMS", "MAX_TERM_LENGTH", "Term", "like_pattern", "match_expression", "terms_of"]

MAX_TERMS = 8
MAX_TERM_LENGTH = 100
COLUMNS = "{title names}"  # the only columns a query may reach
_CJK = re.compile("[ᄀ-ᇿ⺀-〿぀-ヿ㄀-ㄯ㄰-㆏ㇰ-ㇿ㐀-䶿一-鿿가-힯豈-﫿ｦ-ﾟ]")


class Term(str):
    """One word of the query, normalised (NFC, no control character)."""

    @property
    def cjk(self) -> bool:
        return _CJK.search(self) is not None


def terms_of(query: str) -> list[Term]:
    text = unicodedata.normalize("NFC", query)
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    found: list[Term] = []
    for word in text.split():
        word = word[:MAX_TERM_LENGTH]
        if any(unicodedata.category(c)[0] in "LNM" for c in word):
            found.append(Term(word))
        if len(found) == MAX_TERMS:
            break
    return found


def _phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def match_expression(term: Term, *, trigram: bool) -> str:
    """The MATCH argument for one word: a prefix on the word table, a substring on the trigram."""
    phrase = _phrase(term)
    return f"{COLUMNS} : {phrase}" if trigram else f"{COLUMNS} : {phrase} *"


def like_pattern(term: str) -> str:
    """A `LIKE` pattern (with `ESCAPE '\\'`) for a substring: the wildcards are text."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
