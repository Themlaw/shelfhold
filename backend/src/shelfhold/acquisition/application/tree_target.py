"""Where the chapters of an existing Suwayomi download tree go (EF-INT-009).

A tree never makes a Work: a series folder is matched to a Suwayomi binding that already exists
(the one a follow made), by its title, and its chapters become Units of that binding's Edition. A
series with no binding, or with two, is left alone and said so: nothing is guessed, and a Work that
was purged (its bindings are gone, its tombstones stay) is never brought back by a folder on a
disk. A series the tombstones name is refused as well, even if a binding of it was made again.

A chapter whose number the Edition already holds with a stored copy is a duplicate: it is skipped,
never stored as a second Variant. A Unit the follow already knows but never downloaded takes the
file into its own Variant, which is the point of the import (no download again). Anything else is
a new Unit through `Catalog.ingest`, under a reference of its own.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import (
    BindingRef,
    Catalog,
    IncomingUnit,
    content_warning_age,
    effective_age,
    numbered_mark,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import acquisition, catalog, storage
from shelfhold.library.public import SeriesBindingRef, TombstoneGate
from shelfhold.sdk.age import X_MIN_AGE, AgeRating

__all__ = [
    "SUWAYOMI_PLUGIN",
    "SeriesBinding",
    "TreeTargets",
    "chapter_number",
    "fold",
    "series_key",
    "source_suggestion",
]

SUWAYOMI_PLUGIN = "suwayomi"
_KNOWN_WARNINGS = frozenset({"safe", "mixed", "nsfw"})
_NUMBER = re.compile(
    r"(?:(?<![^\W_])(?:ch(?:apter|\.)?|chap\.?|ep(?:isode|\.)?|#)\s*)(\d{1,5}(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)


def series_key(title: str) -> str:
    """What two spellings of a series title share: Suwayomi writes the folder name with the
    characters a file system refuses replaced, so only letters and digits are compared."""
    folded = unicodedata.normalize("NFKC", title).casefold()
    return "".join(c for c in folded if c.isalnum())


def chapter_number(name: str) -> str | None:
    """`Ch.12.5`, `Chapter 12`, `Team_Chapter 12 - title` -> `12.5`, `12`. A name that does not
    say "chapter" (a volume, an extra, a bonus, a one-shot, a year) is unnumbered (None): a
    number is never guessed out of it."""
    found = _NUMBER.search(name.removesuffix(".cbz"))
    return found.group(1) if found else None


def _age(minimum: int | None, x: bool) -> AgeRating | None:
    if x:
        return AgeRating(X_MIN_AGE, x=True)
    return None if minimum is None else AgeRating(minimum)


def fold(text: str) -> str:
    """NFKC, case-folded, spaces reduced to one: nothing else is dropped, a dot inside a name
    makes another name."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _folders(name: str | None, lang: str | None) -> frozenset[str]:
    """How Suwayomi names the folder of a source: `Name (LANG)`, or the name alone."""
    if not name or not fold(name):
        return frozenset()
    spellings = [name, f"{name} ({lang})" if lang else name]
    return frozenset(fold(x) for x in spellings)


def source_suggestion(warning: str | None) -> AgeRating | None:
    """The age a sub-source's `content_warning` suggests, by the very rule of the follow cycle
    (`content_warning_age`: `safe` nothing, `mixed` 18+, `nsfw` X). Case and spaces are
    normalised; a value outside that domain, or none, is not known to be safe: it counts for
    18+."""
    word = fold(warning or "")
    if word not in _KNOWN_WARNINGS:
        return AgeRating(X_MIN_AGE)
    return content_warning_age(word)


@dataclass(frozen=True)
class SeriesBinding:
    binding_id: str
    edition_id: str
    source_key: str
    remote_id: str
    title: str
    folders: frozenset[str] = frozenset()  # the keys of the `<source>` folder names it may have
    age: AgeRating | None = None  # the most restrictive age of its Work and Edition
    suggested: AgeRating | None = None  # what its source's content warning suggests


class TreeTargets:
    def __init__(self, engine: Engine, clock: Clock, gate: TombstoneGate) -> None:
        self._engine = engine
        self._clock = clock
        self._gate = gate

    # -- the series ---------------------------------------------------------------------------

    def bindings(self) -> dict[str, list[SeriesBinding]]:
        """The Suwayomi bindings by the key of their series title."""
        binding, item, work = acquisition.source_binding, acquisition.source_item, catalog.work
        edition, sub = catalog.edition, acquisition.sub_source
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    binding.c.id,
                    binding.c.edition_id,
                    item.c.source_key,
                    item.c.remote_id,
                    work.c.title,
                    work.c.age_rating,
                    work.c.age_x,
                    edition.c.age_rating.label("e_age"),
                    edition.c.age_x.label("e_x"),
                    sub.c.name.label("s_name"),
                    sub.c.lang.label("s_lang"),
                    sub.c.content_warning.label("s_warning"),
                )
                .select_from(
                    binding.join(item, item.c.id == binding.c.source_item_id)
                    .join(work, work.c.id == binding.c.work_id)
                    .join(edition, edition.c.id == binding.c.edition_id)
                    .outerjoin(
                        sub,
                        sa.and_(
                            sub.c.plugin_id == item.c.plugin_id,
                            sub.c.source_key == item.c.source_key,
                        ),
                    )
                )
                .where(item.c.plugin_id == SUWAYOMI_PLUGIN)
                .order_by(binding.c.id)
            ).all()
        found: dict[str, list[SeriesBinding]] = {}
        for row in rows:
            warning = row.s_warning
            found.setdefault(series_key(row.title), []).append(
                SeriesBinding(
                    row.id,
                    row.edition_id,
                    row.source_key,
                    row.remote_id,
                    row.title,
                    folders=_folders(row.s_name, row.s_lang),
                    age=effective_age(_age(row.age_rating, row.age_x), [_age(row.e_age, row.e_x)]),
                    suggested=source_suggestion(warning),
                )
            )
        return found

    def blocked(self, series: SeriesBinding) -> bool:
        ref = SeriesBindingRef(SUWAYOMI_PLUGIN, series.source_key, series.remote_id)
        return bool(self._gate.check(ref, root_id=None).blocked)

    # -- a chapter ----------------------------------------------------------------------------

    def duplicate(self, series: SeriesBinding, number: str) -> bool:
        """The Edition already holds a stored copy of this chapter number."""
        mark = numbered_mark(number)
        if mark is None:
            return False
        with self._engine.connect() as connection:
            return any(
                self._has_content(connection, unit_id)
                for unit_id in self._units_at(connection, series.edition_id, mark)
            )

    @staticmethod
    def _units_at(connection: Connection, edition_id: str, mark: tuple[str, str]) -> list[str]:
        unit = catalog.unit
        return list(
            connection.execute(
                sa.select(unit.c.id).where(
                    unit.c.edition_id == edition_id,
                    unit.c.track == "main",
                    unit.c.number_segment == mark[0],
                    unit.c.number_norm == mark[1],
                    unit.c.part == "",
                )
            ).scalars()
        )

    @staticmethod
    def _has_content(connection: Connection, unit_id: str) -> bool:
        return (
            connection.execute(
                sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit_id).limit(1)
            ).first()
            is not None
        )

    def target(
        self, series: SeriesBinding, chapter_ref: str, number: str, title: str
    ) -> tuple[str, str]:
        """(unit id, variant id) to store the file under."""
        mark = numbered_mark(number)
        now = self._clock.now()
        with self._engine.connect() as connection:
            connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with connection.begin():
                if mark is not None:
                    for unit_id in self._units_at(connection, series.edition_id, mark):
                        variant = self._bare_variant(connection, unit_id, series.binding_id)
                        if variant is not None:
                            return unit_id, variant
                result = Catalog(connection).ingest(
                    IncomingUnit(
                        binding=BindingRef(series.binding_id, series.edition_id),
                        remote_unit_id=f"{series.source_key}:tree/{chapter_ref}",
                        raw_number=number,
                        seen_at=now,
                        title=title,
                        kind="chapter",
                    )
                )
        return result.unit_id, result.variant_id

    @staticmethod
    def _bare_variant(connection: Connection, unit_id: str, binding_id: str) -> str | None:
        """A Variant of this binding that holds no file yet: the follow knew the chapter."""
        variant = catalog.unit_variant
        stored = sa.select(storage.content.c.id).where(storage.content.c.variant_id == variant.c.id)
        return connection.execute(
            sa.select(variant.c.id)
            .where(
                variant.c.unit_id == unit_id,
                variant.c.source_binding_id == binding_id,
                variant.c.removed_at.is_(None),
                ~stored.exists(),
            )
            .order_by(variant.c.id)
            .limit(1)
        ).scalar()
