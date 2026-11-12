"""The subject and word filter of the family search (EF-USR-018, M3 L19).

A remote result of a family source has no rating. Before a child may see even its title, this
filter drops what carries a flagged subject or word: in the title, or in the subjects the source
says (`memo["subjects"]`). The default list always holds; a profile can add its own terms, never
remove one. Matching is on whole words after folding case, accents and width, so "Essex" is not
"sex". Closed by default: subjects that cannot be read (not text, not a list of text) flag the
result, because a source that sends something odd has not told us it is clean.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Final

__all__ = ["DEFAULT_TERMS", "MAX_TERMS", "MAX_TERM_CHARS", "clean_terms", "is_flagged"]

MAX_TERMS: Final = 100
MAX_TERM_CHARS: Final = 64
MAX_SUBJECTS: Final = 200

# English and French: the sources of a family list speak both. Words, or short phrases.
DEFAULT_TERMS: Final = (
    "erotica", "erotic", "erotique", "erotiques", "erotisme", "porn", "porno", "pornography",
    "pornographie", "pornographic", "sex", "sexe", "sexual", "sexuel", "sexuelle", "sexuality",
    "sexualite", "hentai", "bdsm", "fetish", "fetiche", "nsfw", "xxx", "lust", "libertinage",
)  # fmt: skip

_NOT_WORD = re.compile(r"[^0-9a-z]+")


def _fold(text: str) -> str:
    """Words of `text`, lowercase, without accents, one space apart."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NOT_WORD.sub(" ", stripped.casefold()).strip()


def clean_terms(raw: object) -> tuple[str, ...]:
    """The terms of a profile: text only, folded, unique, bounded; anything else is dropped."""
    if not isinstance(raw, list | tuple):
        return ()
    found: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        term = _fold(item[: MAX_TERM_CHARS * 4])[:MAX_TERM_CHARS].strip()
        if term and term not in found:
            found.append(term)
        if len(found) >= MAX_TERMS:
            break
    return tuple(found)


def _subjects(memo: Mapping[str, object]) -> list[str] | None:
    """The subjects a source gave, or None when what it gave cannot be read."""
    if "subjects" not in memo:
        return []
    raw = memo["subjects"]
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list | tuple) and len(raw) <= MAX_SUBJECTS:
        texts = [s for s in raw if isinstance(s, str)]
        if len(texts) == len(raw):
            return texts
    return None


def _holds(words: str, term: str) -> bool:
    return f" {term} " in f" {words} "


def is_flagged(title: str, memo: Mapping[str, object], terms: Iterable[str] = ()) -> bool:
    """Whether this remote result is dropped: a flagged word in the title or in a subject."""
    subjects = _subjects(memo)
    if subjects is None:
        return True
    wanted = [*DEFAULT_TERMS, *clean_terms(list(terms))]
    for text in (title, *subjects):
        words = _fold(text)
        if any(_holds(words, term) for term in wanted):
            return True
    return False
