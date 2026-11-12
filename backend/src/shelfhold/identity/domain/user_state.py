"""What a user keeps about a Work (EF-USR-007) and the language they like titles in (EF-BIB-022).

Pure rules: the flags a user sets, the shape of a language preference, and the choice of a title
among the ones a Work carries. Which Works a user may see is decided elsewhere and before any of
this runs; nothing here can show a title the caller was not handed.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

MAX_LANGUAGES: Final = 8
_WILDCARDS: Final = frozenset({"all", "multi", "und", "mul", "zxx"})
_TAG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}")
_KIND_RANK: Final = {"localized": 0, "alt": 1, "romanized": 2}


class UserStateError(Exception):
    """A refusal with a stable code; the text never carries what the caller sent."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class UserFlags:
    followed: bool = False
    favorite: bool = False
    read_at: datetime | None = None


def canonical_tag(tag: str) -> str:
    """`EN-us` -> `en-US`: the language in lower case, a two-letter region in upper case, a
    four-letter script in title case."""
    head, *rest = tag.split("-")
    parts = [head.lower()]
    for part in rest:
        if len(part) == 2 and part.isalpha():
            parts.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            parts.append(part.title())
        else:
            parts.append(part.lower())
    return "-".join(parts)


def normalise_languages(raw: object) -> list[str]:
    """A short ordered list of language tags: no wildcard, no junk, no repeat (`en` = `EN`)."""
    if not isinstance(raw, list | tuple) or len(raw) > MAX_LANGUAGES:
        raise UserStateError("invalid_languages")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or len(item) > 35 or not _TAG.fullmatch(item):
            raise UserStateError("invalid_languages")
        tag = canonical_tag(item)
        if tag.split("-")[0] in _WILDCARDS:
            raise UserStateError("invalid_languages")
        if tag.lower() not in (seen.lower() for seen in out):
            out.append(tag)
    return out


def pick_title(
    languages: Sequence[str], own: str, titles: Sequence[tuple[str, str, str]]
) -> tuple[str, str | None]:
    """The title in the first preferred language, else the Work's own. `titles` are
    `(lang, kind, title)`; a preference `en-US` falls back to `en`. Returns `(title, lang)`."""
    ranked = sorted(titles, key=lambda t: (_KIND_RANK.get(t[1], 9), t[2]))
    for wanted in languages:
        primary = wanted.split("-")[0].lower()
        for exact in (True, False):
            for lang, _kind, title in ranked:
                have = lang.lower()
                if (have == wanted.lower()) if exact else (have.split("-")[0] == primary):
                    return title, lang
    return own, None
