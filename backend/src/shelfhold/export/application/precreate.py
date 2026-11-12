"""The shelves a new instance starts with, and one per adopted Kavita library (EF-UX-007).

"Manga", "Comics et BD", "Livres" and "Famille" (13+ at most) are made once; the kinds of Works
get their default shelf from them. Each library adopted from Kavita gets a shelf of its own, named
like it. The whole thing is idempotent: a shelf that exists, whatever its settings, is left as it
is, and so is a default a kind already has. It creates, never rewrites, so it needs no reserved
action beyond the creation of a shelf.

Linking "Famille" to its dedicated library goes through `export/targets/kavita/linking.py`.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.export.domain.bindings import (
    FAMILY_SHELF_MAX_AGE,
    FAMILY_SHELF_NAME,
    check_family_cap,
    is_family,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export

__all__ = ["PRECREATED", "AdoptedLibrary", "Precreated", "precreate_shelves"]


@dataclass(frozen=True)
class _Spec:
    name: str
    phrase: str
    kinds: tuple[str, ...]  # the kinds whose default shelf this one is
    max_age_rating: int | None = None


PRECREATED: tuple[_Spec, ...] = (
    _Spec(
        "Manga",
        "Les mangas vont dans Kavita \u203a Manga",
        ("manga", "manhwa", "manhua", "webtoon"),
    ),
    _Spec(
        "Comics et BD", "Les comics et les BD vont dans Kavita \u203a Comics et BD", ("comic", "bd")
    ),
    _Spec("Livres", "Les livres vont dans Kavita \u203a Livres", ("light_novel", "novel", "ebook")),
    # no kind defaults to it: a child's shelf is chosen, never proposed (EF-EXP-002, EF-BIB-055)
    _Spec(
        FAMILY_SHELF_NAME,
        "Ce que les enfants lisent va dans Kavita \u203a Famille (13 ans au plus)",
        (),
        FAMILY_SHELF_MAX_AGE,
    ),
)


@dataclass(frozen=True)
class AdoptedLibrary:
    name: str  # the name of the library in Kavita
    kind: str  # the kind of Works it holds, a key of `work_kind`: it defaults there if none does


@dataclass(frozen=True)
class Precreated:
    created: tuple[str, ...]  # the names of the shelves this call made


def _make(connection: Connection, name: str, phrase: str, ceiling: int | None) -> bool:
    check_family_cap(name, ceiling)
    inserted = connection.execute(
        sqlite_insert(export.shelf)
        .values(
            id=new_ulid(), name=name, phrase=phrase, max_age_rating=ceiling,
            hold_until_rated=is_family(ceiling),
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )  # fmt: skip
    return inserted.rowcount == 1


def _default_for(connection: Connection, kinds: Sequence[str], shelf: str) -> None:
    kind = catalog.work_kind
    connection.execute(
        sa.update(kind)
        .where(kind.c.key.in_(kinds), kind.c.default_shelf.is_(None))
        .values(default_shelf=shelf)
    )


def precreate_shelves(engine: Engine, *, adopted: Sequence[AdoptedLibrary] = ()) -> Precreated:
    """Make the shelves that do not exist yet; lock first, so two runs at once make each once."""
    made: list[str] = []
    with engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as connection:
        for spec in PRECREATED:
            if _make(connection, spec.name, spec.phrase, spec.max_age_rating):
                made.append(spec.name)
            _default_for(connection, spec.kinds, spec.name)
        for library in adopted:
            ceiling = FAMILY_SHELF_MAX_AGE if library.name == FAMILY_SHELF_NAME else None
            phrase = f"Les contenus de « {library.name} » vont dans Kavita \u203a {library.name}"
            if _make(connection, library.name, phrase, ceiling):
                made.append(library.name)
            if ceiling is None:  # a library takes the default of its kind only if none has one
                _default_for(connection, (library.kind,), library.name)
    return Precreated(tuple(made))
