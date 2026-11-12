"""Romanized forms of Japanese titles and names, computed at the indexing (D6, EF-BIB-010).

A reader who types "shingeki" finds "進撃の巨人". The library is `pykakasi` (pure Python, its
dictionary shipped in the wheel, nothing downloaded at run time, GPL-3.0-or-later: compatible with
the AGPL). It is the one adapter of it: a text with no kana and no han character is never sent.
"""

import re
from functools import cache
from typing import Any

__all__ = ["romanized"]

_JAPANESE = re.compile("[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


@cache
def _converter() -> Any:
    from pykakasi import kakasi

    return kakasi()  # type: ignore[no-untyped-call]


def romanized(text: str) -> str:
    """The Hepburn reading of `text`, words apart; empty when it holds no Japanese."""
    if not _JAPANESE.search(text):
        return ""
    words = [part["hepburn"] for part in _converter().convert(text) if part["hepburn"].strip()]
    return " ".join(words)
