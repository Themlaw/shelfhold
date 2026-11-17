"""How two titles are compared (EF-USR-019, the debt of M3 L35 on `_work_by_title`).

A title typed by a person and the title a file was given are the same when they differ only by
what a reader does not see: width and ligatures (NFKC), case (`casefold`, so `ß` is `ss`),
accents (the combining marks of the decomposed form) and the spacing. Nothing is transliterated:
a title in another script keeps its letters, so two different titles never collapse into one
empty string, which an ASCII reduction would do.
"""

import unicodedata

__all__ = ["fold_title"]


def fold_title(text: str) -> str:
    """The comparison form of a title; the empty string stays empty."""
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text).casefold())
    bare = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    # casefolding can create a new composed accent (`İ` -> `i` + a mark): normalize once more
    return " ".join(unicodedata.normalize("NFKC", bare).casefold().split())
