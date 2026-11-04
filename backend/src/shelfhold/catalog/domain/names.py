"""Noms de contributeurs (EF-BIB-046, invariant I21).

Un champ de personne exporté (ComicInfo `Writer`…) ne contient jamais de virgule (Kavita coupe à la
virgule) et jamais le nom de tri (`sort`), qui ne sert qu'à `file-as` dans l'OPF.
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
    """Aucun nom utilisable dans un champ de personne (il n'existe que des noms de tri)."""


_PREFERENCE = (NameKind.DISPLAY, NameKind.NATIVE, NameKind.ROMANIZED)
_AUTHORITY = re.compile(r"^([^,]+),\s*([^,]+)$")


def _primary(lang: str) -> str:
    return lang.split("-", 1)[0].casefold()


def person_field_name(
    names: Sequence[ContributorName], lang: str | None = None, *, is_org: bool = False
) -> PersonField:
    """Forme d'affichage d'un contributeur pour un champ de personne.

    Ordre de préférence : la langue demandée (`display`, `native`, `romanized`), puis les autres
    langues. Une forme d'autorité « Nom, Prénom » d'une personne passe à l'ordre naturel ; toute
    autre virgule est remplacée (et signalée pour le journal).
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
    """Plusieurs personnes dans un champ ComicInfo : jointes par `, `."""
    return ", ".join(names)
