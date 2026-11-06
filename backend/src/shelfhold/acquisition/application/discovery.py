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

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.acquisition.application.sub_sources import upsert_sub_sources
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.acquisition.domain.watermark import Watermark
from shelfhold.catalog.public import (
    Catalog,
    EditionSpec,
    LanguageError,
    edition_key_lang,
    numbered_mark,
    translation_type_for,
)
from shelfhold.export.public import forget_coverage_unknown, pending_pairing
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust import Destination, sanitize_upstream_text
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from shelfhold.library.public import SeriesBindingRef, TombstoneGate
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.source import SubSource

__all__ = ["Discovery", "Notices", "SeriesDiscovery", "decline_pairing"]

log = structlog.get_logger(__name__)

WORK_KIND = "manga"
UNKNOWN_LANGUAGE = "x-unknown"  # `edition set-lang` leaves it (language_unknown)
TITLE_MAX = 200
ADULT_FROM = 18  # a ceiling of 18 or more, or none, is an adult shelf (M1a has no other)

Emit = Callable[[sa.Connection, str, dict[str, object]], None]
ListUnits = Callable[[SeriesRef], Awaitable[list[UnitInfo]]]
SubSources = Callable[[], Awaitable[list[SubSource]]]


def digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class Notices:
    """What the cycle has told and what waits, kept in `follow_notice`: a new process (the job
    builds a `FollowCycle` each time) never tells twice, and a condition that ceases leaves."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def sync(self, connection: sa.Connection, kind: str, current: Mapping[str, str]) -> list[str]:
        """Make the rows of `kind` equal `current` (`key_hash -> subject`); the keys new now."""
        table = acquisition.follow_notice
        held = set(
            connection.execute(sa.select(table.c.key_hash).where(table.c.kind == kind)).scalars()
        )
        gone = held - current.keys()
        if gone:
            connection.execute(
                sa.delete(table).where(table.c.kind == kind, table.c.key_hash.in_(gone))
            )
        fresh = [key for key in current if key not in held]
        for key in fresh:
            connection.execute(
                sa.insert(table).values(
                    kind=kind, key_hash=key, subject=current[key], told_at=self._clock.now()
                )
            )
        return fresh

    @staticmethod
    def declined(connection: sa.Connection) -> set[str]:
        table = acquisition.follow_notice
        return set(
            connection.execute(
                sa.select(table.c.subject).where(table.c.kind == "pairing_declined")
            ).scalars()
        )


def decline_pairing(engine: sa.Engine, clock: Clock, export_series_id: str) -> None:
    """The admin says an adopted series has no twin in the library: what looks like one is
    discovered as a series of its own from the next cycle on."""
    table = acquisition.follow_notice
    with engine.begin() as connection:
        connection.execute(
            sqlite_insert(table)
            .values(
                kind="pairing_declined",
                key_hash=digest(export_series_id),
                subject=export_series_id,
                told_at=clock.now(),
            )
            .on_conflict_do_nothing()
        )
        forget_coverage_unknown(connection, export_series_id)


@dataclass
class Discovery:
    created: int = 0
    tombstoned: int = 0
    awaiting_shelf: int = 0
    skipped_pending_pairing: int = 0
    created_unknown_language: int = 0  # filed, `language_unknown` until `edition set-lang`
    units: dict[str, list[UnitInfo]] = field(default_factory=dict)  # binding -> its listing


class SeriesDiscovery:
    def __init__(
        self,
        engine: sa.Engine,
        clock: Clock,
        plugin_id: str,
        tombstones: TombstoneGate,
        emit: Emit,
        translation_nature: str = "unknown",
    ) -> None:
        self._engine = engine
        self._nature = translation_nature
        self._clock = clock
        self._plugin_id = plugin_id
        self._tombstones = tombstones
        self._emit = emit
        self._notices = Notices(clock)

    async def run(
        self, library: Sequence[SeriesRef], list_units: ListUnits, sub_sources: SubSources
    ) -> Discovery:
        found = Discovery()
        subs = await sub_sources()
        with self._engine.begin() as connection:  # kept every cycle, whatever is left to create
            upsert_sub_sources(connection, self._plugin_id, subs)
        with self._engine.connect() as connection:
            known = self._known(connection)
            fresh = [r for r in library if self._key(r) not in known]
            declined = Notices.declined(connection)
            twins = pending_pairing(connection, fresh, declined=declined) if fresh else {}
            shelf_id = self._adult_shelf(connection)
        todo: list[SeriesRef] = []
        buried: dict[str, str] = {}
        for ref in fresh:
            if self._tombstoned(ref):
                found.tombstoned += 1
                buried[self._digest(ref)] = ""
            elif self._key(ref) in twins:
                found.skipped_pending_pairing += 1
            else:
                todo.append(ref)
        waiting = {
            self._digest(ref): twins[self._key(ref)] for ref in fresh if self._key(ref) in twins
        }
        self._sync(buried, waiting, awaiting=bool(todo) and shelf_id is None)
        if shelf_id is None:
            found.awaiting_shelf = len(todo)
        if not todo or shelf_id is None:
            return found
        langs = {s.source_key: s.lang for s in subs}
        adult_only = {s.source_key for s in subs if s.content_warning == "nsfw"}
        if self._nature == "unknown":  # the kind then decides the discriminant (EF-BIB-049)
            log.info("discovery.translation_nature_unknown", plugin_id=self._plugin_id)
        for ref in todo:
            lang = self._language(langs.get(ref.source_key))
            try:
                units = await list_units(ref)
            except Exception as error:
                failure_class = classify(error)
                if disposition_of(failure_class) is Disposition.PAUSE:
                    raise  # the service cannot be driven: the whole cycle pauses
                log.warning("follow.discovery_skipped", failure_class=failure_class)
                continue  # that series' problem: found again at the next cycle
            binding_id = self._create(
                ref, lang, shelf_id, _watermark(units), adult_only=ref.source_key in adult_only
            )
            if binding_id is not None:
                found.created += 1
                found.created_unknown_language += int(lang is None)
                found.units[binding_id] = units
        return found

    # -- what is told, what waits -------------------------------------------------------------
    def _sync(self, buried: dict[str, str], waiting: dict[str, str], *, awaiting: bool) -> None:
        """Persist the conditions (so no process tells them twice) and announce the new ones."""
        with self._engine.begin() as connection:
            for _ in self._notices.sync(connection, "tombstoned", buried):
                self._emit(connection, "series.tombstoned_upstream", {"count": 1})
            for _ in self._notices.sync(
                connection, "awaiting_shelf", {digest("shelf"): ""} if awaiting else {}
            ):
                self._emit(connection, "series.awaiting_shelf", {"count": 1})
            self._notices.sync(connection, "pending_pairing", waiting)

    # -- what exists ------------------------------------------------------------------------
    def _key(self, ref: SeriesRef) -> tuple[str, str]:
        return (ref.source_key, ref.remote_id)

    def _digest(self, ref: SeriesRef) -> str:
        return digest(self._plugin_id, ref.source_key, ref.remote_id)

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
        return bool(self._tombstones.check(candidate, root_id=None).blocked)

    @staticmethod
    def _language(lang: str | None) -> str | None:
        """The language of the sub-source as an Edition key allows it; `None` for `mul`, for a
        sub-source the service did not list and for a tag without a script: the Edition is then
        filed as `x-unknown` and waits for `edition set-lang` (`language_unknown`)."""
        if lang is None or lang == "mul":
            return None
        try:
            return str(edition_key_lang(lang))
        except LanguageError:
            return None

    @staticmethod
    def _title(ref: SeriesRef) -> str:
        """An upstream title is untrusted text: cleaned, bounded, and the remote identifier when
        nothing is left of it."""
        for text in (ref.title, ref.remote_id):
            clean = sanitize_upstream_text(
                text, destination=Destination.PLAIN, max_length=TITLE_MAX
            )
            if clean.strip():
                return clean.strip()
        return "?"

    # -- the five rows, in one transaction ------------------------------------------------------
    def _create(
        self, ref: SeriesRef, lang: str | None, shelf_id: str, mark: Watermark | None,
        *, adult_only: bool = False,
    ) -> str | None:  # fmt: skip
        """Work, Edition, `source_item`, binding and follow, all or none. Read again under the
        write lock: two cycles racing leave one of each. `None`: another cycle got there first."""
        now = self._clock.now()
        translation_type = translation_type_for(self._nature)
        with self._engine.connect() as connection:
            connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with connection.begin():
                if self._key(ref) in self._known(connection):
                    return None
                books = Catalog(connection)
                work_id = books.create_work(WORK_KIND, self._title(ref))
                if lang is None:  # no key can carry it: the shape adoption uses, set-lang leaves it
                    edition_id = new_ulid()
                    connection.execute(
                        sa.insert(catalog.edition).values(
                            id=edition_id, work_id=work_id, lang=UNKNOWN_LANGUAGE,
                            translation_type=translation_type, media="images",
                        )
                    )  # fmt: skip
                else:
                    edition_id = books.ensure_edition(
                        EditionSpec(
                            work_id, lang, translation_type=translation_type, media="images"
                        )
                    ).id
                connection.execute(
                    sa.update(catalog.edition)
                    .where(catalog.edition.c.id == edition_id)
                    .values(shelf_id=shelf_id)
                )
                if adult_only:  # the sub-source says X (`isNsfw`): a suggestion, never a rating
                    connection.execute(
                        sa.insert(catalog.age_suggestion).values(
                            id=new_ulid(), entity_type="edition", entity_id=edition_id,
                            value=ADULT_FROM, x=True, provenance="sub_source", seen_at=now,
                        )
                    )  # fmt: skip
                item_id = self._source_item(connection, ref, now)
                binding_id = new_ulid()
                connection.execute(
                    sa.insert(acquisition.source_binding).values(
                        id=binding_id, work_id=work_id, edition_id=edition_id,
                        source_item_id=item_id, priority=1, active=True,
                    )
                )  # fmt: skip
                follow = acquisition.series_follow
                # No numbered chapter yet: the watermark stays null, the first numbered look sets it
                values = (
                    {}
                    if mark is None
                    else {
                        "follow_from_segment": mark.segment,
                        "follow_from_number": format(mark.number, "f"),
                        "follow_from_norm_by": "number@1",
                        "updated_at": now,
                    }
                )
                connection.execute(
                    sqlite_insert(follow)
                    .values(edition_id=edition_id, work_id=work_id, **values)
                    .on_conflict_do_nothing(index_elements=["edition_id"])
                )
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


def _watermark(units: Sequence[UnitInfo]) -> Watermark | None:
    """The highest numbered chapter visible, `(segment, number)`: a more recent segment is beyond
    whatever its number (I34). `None` while nothing is numbered: the first numbered look then
    takes the watermark (the archive that shows up late is not asked for)."""
    marks = [m for u in units if (m := numbered_mark(u.number, u.source_order)) is not None]
    if not marks:
        return None
    return max(
        (Watermark.of(segment, Decimal(number)) for segment, number in marks),
        key=lambda w: (w.ordinal, w.number),
    )
