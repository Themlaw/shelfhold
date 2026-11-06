"""Discovering the series of the Suwayomi library (EF-SRC-009, EF-BIB-053 (2), EF-SRC-036).

The library of the service decides which series are followed. A series of it that has no binding
yet (a fresh installation, a series added in Suwayomi since) gets, in **one** transaction, its
Work, its Edition (the language of its sub-source, filed on the adult shelf), its `source_item`,
its binding and its follow, whose watermark is the highest chapter visible: the first cycle asks
for what is new, never for the archive (EF-EXP-053).

Three series are left alone: one under a `series_binding` tombstone (never created), one that an
adopted, still unpaired series would be paired with (the pairing binds it to the Edition that
exists), and one that has nowhere to go (no adult shelf: `series.awaiting_shelf`, told once; its
catalog entry carries the command that leaves the wait: `shelf create`).
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.acquisition.application.planning import NOTHING_YET
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.acquisition.domain.watermark import Watermark
from shelfhold.catalog.public import (
    Catalog,
    EditionSpec,
    LanguageError,
    edition_key_lang,
    numbered_mark,
)
from shelfhold.export.public import pending_pairing
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from shelfhold.library.public import SeriesBindingRef, TombstoneGate
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.source import SubSource

__all__ = ["Discovery", "SeriesDiscovery"]

log = structlog.get_logger(__name__)

WORK_KIND = "manga"
ADULT_FROM = 18  # a ceiling of 18 or more, or none, is an adult shelf (M1a has no other)

Emit = Callable[[sa.Connection, str, dict[str, object]], None]
ListUnits = Callable[[SeriesRef], Awaitable[list[UnitInfo]]]
SubSources = Callable[[], Awaitable[list[SubSource]]]


@dataclass
class Discovery:
    created: int = 0
    tombstoned: int = 0
    awaiting_shelf: int = 0
    skipped_pending_pairing: int = 0
    skipped_language: int = 0
    fresh: set[str] = field(default_factory=set)  # bindings created now: their first look


class SeriesDiscovery:
    def __init__(
        self, engine: sa.Engine, clock: Clock, plugin_id: str, tombstones: TombstoneGate, emit: Emit
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._plugin_id = plugin_id
        self._tombstones = tombstones
        self._emit = emit
        self._told: set[tuple[str, str]] = set()  # tombstoned series already announced
        self._waiting = False  # the lack of an adult shelf is already announced

    async def run(
        self, library: Sequence[SeriesRef], list_units: ListUnits, sub_sources: SubSources
    ) -> Discovery:
        found = Discovery()
        with self._engine.connect() as connection:
            known = self._known(connection)
            twins = pending_pairing(connection, [r for r in library if self._key(r) not in known])
            shelf_id = self._adult_shelf(connection)
        fresh = [r for r in library if self._key(r) not in known]
        todo: list[SeriesRef] = []
        for ref in fresh:
            if self._tombstoned(ref):
                found.tombstoned += 1
            elif self._key(ref) in twins:
                found.skipped_pending_pairing += 1
                log.info("follow.discovery_left_to_pairing", source_key=ref.source_key)
            else:
                todo.append(ref)
        if not todo:
            return found
        if shelf_id is None:
            found.awaiting_shelf = len(todo)
            self._tell_waiting(len(todo))
            return found
        self._waiting = False
        langs = {s.source_key: s.lang for s in await sub_sources()}
        for ref in todo:
            lang = self._language(langs.get(ref.source_key))
            if lang is None:
                found.skipped_language += 1
                log.warning("follow.discovery_language_unknown", source_key=ref.source_key)
                continue
            try:
                units = await list_units(ref)
            except Exception as error:
                failure_class = classify(error)
                if disposition_of(failure_class) is Disposition.PAUSE:
                    raise  # the service cannot be driven: the whole cycle pauses
                log.warning("follow.discovery_skipped", failure_class=failure_class)
                continue  # that series' problem: found again at the next cycle
            binding_id = self._create(ref, lang, shelf_id, _watermark(units))
            if binding_id is not None:
                found.created += 1
                found.fresh.add(binding_id)
        return found

    # -- what exists ------------------------------------------------------------------------
    def _key(self, ref: SeriesRef) -> tuple[str, str]:
        return (ref.source_key, ref.remote_id)

    def _known(self, connection: sa.Connection) -> set[tuple[str, str]]:
        """Series that have a binding, whatever its state: a removed one is reactivated, never
        created a second time."""
        item, binding = acquisition.source_item, acquisition.source_binding
        rows = connection.execute(
            sa.select(item.c.source_key, item.c.remote_id)
            .select_from(binding.join(item, item.c.id == binding.c.source_item_id))
            .where(item.c.plugin_id == self._plugin_id)
        )
        return {(r.source_key, r.remote_id) for r in rows}

    @staticmethod
    def _adult_shelf(connection: sa.Connection) -> str | None:
        """The adult shelf that has an enabled destination: the first one created."""
        shelf, destination = export.shelf, export.shelf_destination
        return connection.execute(
            sa.select(shelf.c.id)
            .join(destination, destination.c.shelf_id == shelf.c.id)
            .where(
                destination.c.enabled,
                sa.or_(shelf.c.max_age_rating.is_(None), shelf.c.max_age_rating >= ADULT_FROM),
            )
            .order_by(shelf.c.id)
            .limit(1)
        ).scalar()

    def _tombstoned(self, ref: SeriesRef) -> bool:
        candidate = SeriesBindingRef(self._plugin_id, ref.source_key, ref.remote_id)
        if not self._tombstones.check(candidate, root_id=None).blocked:
            return False
        if self._key(ref) not in self._told:
            self._told.add(self._key(ref))
            with self._engine.begin() as connection:
                self._emit(connection, "series.tombstoned_upstream", {"count": 1})
        return True

    def _tell_waiting(self, count: int) -> None:
        if self._waiting:
            return
        self._waiting = True
        with self._engine.begin() as connection:
            self._emit(connection, "series.awaiting_shelf", {"count": count})

    @staticmethod
    def _language(lang: str | None) -> str | None:
        """The language of the sub-source as an Edition key allows it; `None` for `mul`, for a
        sub-source the service did not list and for a tag without a script."""
        if lang is None or lang == "mul":
            return None
        try:
            return str(edition_key_lang(lang))
        except LanguageError:
            return None

    # -- the five rows, in one transaction ------------------------------------------------------
    def _create(self, ref: SeriesRef, lang: str, shelf_id: str, mark: Watermark) -> str | None:
        """Work, Edition, `source_item`, binding and follow, all or none. Read again under the
        write lock: two cycles racing leave one of each. `None`: another cycle got there first."""
        now = self._clock.now()
        with self._engine.connect() as connection:
            connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with connection.begin():
                if self._key(ref) in self._known(connection):
                    return None
                books = Catalog(connection)
                work_id = books.create_work(WORK_KIND, ref.title)
                edition = books.ensure_edition(
                    EditionSpec(work_id, lang, translation_type="official", media="images")
                )
                connection.execute(
                    sa.update(catalog.edition)
                    .where(catalog.edition.c.id == edition.id)
                    .values(shelf_id=shelf_id)
                )
                item_id = self._source_item(connection, ref, now)
                binding_id = new_ulid()
                connection.execute(
                    sa.insert(acquisition.source_binding).values(
                        id=binding_id, work_id=work_id, edition_id=edition.id,
                        source_item_id=item_id, priority=1, active=True,
                    )
                )  # fmt: skip
                follow = acquisition.series_follow
                connection.execute(
                    sqlite_insert(follow)
                    .values(
                        edition_id=edition.id, work_id=work_id, follow_from_segment=mark.segment,
                        follow_from_number=format(mark.number, "f"),
                        follow_from_norm_by="number@1", updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["edition_id"])
                )  # fmt: skip
                return binding_id

    def _source_item(self, connection: sa.Connection, ref: SeriesRef, now: object) -> str:
        item = acquisition.source_item
        found = connection.execute(
            sa.select(item.c.id).where(
                item.c.plugin_id == self._plugin_id,
                item.c.source_key == ref.source_key,
                item.c.remote_id == ref.remote_id,
            )
        ).scalar()
        if found:
            return str(found)
        item_id = new_ulid()
        connection.execute(
            sa.insert(item).values(
                id=item_id, plugin_id=self._plugin_id, source_key=ref.source_key,
                remote_id=ref.remote_id, memo=ref.memo, memo_version=ref.memo_version,
                last_seen=now,
            )
        )  # fmt: skip
        return item_id


def _watermark(units: Sequence[UnitInfo]) -> Watermark:
    """The highest numbered chapter visible, `(segment, number)`: a more recent segment is beyond
    whatever its number (I34). A series that shows only extras starts from nothing."""
    marks = [m for u in units if (m := numbered_mark(u.number, u.source_order)) is not None]
    if not marks:
        return NOTHING_YET
    top = max(
        (Watermark.of(segment, Decimal(number)) for segment, number in marks),
        key=lambda w: (w.ordinal, w.number),
    )
    return top
