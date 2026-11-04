"""Contributor names (EF-BIB-046, invariant I21).

An exported person field (ComicInfo `Writer`...) never holds a comma (Kavita splits on commas) and
never the sort name (`sort`), which only serves OPF `file-as`.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class NameKind(StrEnum):
    DISPLAY = "display"
    NATIVE = "native"
    ROMANIZED = "romanized"
    SORT = "sort"


@dataclass(frozen=True)
class ContributorName:
    name: str
    lang: str
    script: str
    kind: NameKind
    family_first: bool = False


@dataclass(frozen=True)
class PersonField:
    text: str
    commas_replaced: bool


class NoPersonFieldName(ValueError):
    """No name usable in a person field (only sort names exist)."""


_PREFERENCE = (NameKind.DISPLAY, NameKind.NATIVE, NameKind.ROMANIZED)
_AUTHORITY = re.compile(r"^([^,]+),\s*([^,]+)$")


def _primary(lang: str) -> str:
    return lang.split("-", 1)[0].casefold()


def person_field_name(
    names: Sequence[ContributorName], lang: str | None = None, *, is_org: bool = False
) -> PersonField:
    """Display form of a contributor for a person field.

    Preference: the requested language (`display`, `native`, `romanized`), then other languages. An
    authority form "Family, Given" of a person switches to natural order; any other comma is
    replaced (and reported for the log).
    """
    usable = [n for n in names if n.kind is not NameKind.SORT]
    if not usable:
        raise NoPersonFieldName("only sort names are available")

    def rank(n: ContributorName) -> tuple[int, int]:
        same_lang = lang is not None and _primary(n.lang) == _primary(lang)
        return (0 if same_lang else 1, _PREFERENCE.index(n.kind))

    text = min(usable, key=rank).name.strip()
    if not is_org and (match := _AUTHORITY.match(text)):
        text = f"{match.group(2).strip()} {match.group(1).strip()}"
    replaced = "," in text
    if replaced:
        text = re.sub(r"\s+", " ", text.replace(",", " ")).strip()
    return PersonField(text, replaced)


def join_people(names: Iterable[str]) -> str:
    """Several people in one ComicInfo field are joined by `, `."""
    return ", ".join(names)
