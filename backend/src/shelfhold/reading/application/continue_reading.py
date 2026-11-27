"""« Reprendre la lecture » (M5 L14, EF-LEC-008): the next Unit to read of each series under way.

Read from the `reading_progress` projection, which already merges what the Kavita pull said
(`source = server`, on an exported file) and what the integrated reader wrote (`device`, on the kept
copy): two sources that agree are one row per (profile, Unit), so a series is never offered twice.

* One entry per Work. Anchor: the Unit being read (the latest one, when several), else the Unit
  after the furthest one read, in reading order, in the Edition she was reading, among those that
  can be opened. A series whose every Unit is read, or whose next Unit nobody holds, is not offered.
* Visibility is the filter of every other surface (`visible_editions_clause`), applied in the
  query that reads the progress: a series the profile may not see is not an entry, a count, or an
  error: the answer is the one of a profile with no progress. No new rule of visibility.
* An excerpt (D-P1) writes no position (L07), so it never brings a series here.
* `target`: `kept_copy` when the profile reads a kept copy of the Unit in full (ADR-0055 D-P1,
  the rule of the publication), else the kind of the destination she reads the Unit on (« Ouvrir
  dans Komga » in front of a Komga server, J5-S2) — judged by the one query of every surface
  (`content_access.read_elsewhere`), on each content of hers that covers the Unit, then through
  the Unit's coverage (an assembled volume has no content of its own); the entry carries the
  address of the destination that answered, the one her account is on.
* Bounded: `MAX_ENTRIES` at most, `MAX_WORKS_EXAMINED` series looked at (only those with something
  to offer), `MAX_CANDIDATES` Units tried per series, whatever the library.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import sqlalchemy as sa

from shelfhold.catalog.public import visible_edition_ids, visible_editions_clause
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.schema import catalog, reading, storage
from shelfhold.reading.application.content_access import (
    ContentAccess,
    Elsewhere,
    NoReadableCopyError,
    read_elsewhere,
    read_elsewhere_of_unit,
)
from shelfhold.reading.application.edition_progress import EditionProgress, edition_progress
from shelfhold.reading.application.progress import UnknownCarrierError
from shelfhold.reading.application.publication import PublicationService

__all__ = ["DEFAULT_ENTRIES", "MAX_ENTRIES", "ContinueEntry", "ContinueReading"]

DEFAULT_ENTRIES: Final = 12
MAX_ENTRIES: Final = 24
MAX_WORKS_EXAMINED: Final = 120
_BATCH: Final = 40
MAX_CANDIDATES: Final = 8  # Units tried for one series before it waits

Target = str  # `kept_copy`, or the kind of the destination the Unit is read on

# The view `reading_progress` (frozen columns, `docs/reference/reading-progress.md`).
_PROGRESS = sa.table(
    "reading_progress",
    sa.column("user_id"), sa.column("unit_id"), sa.column("state"),
    sa.column("latest_position_id"), sa.column("approximate"),
)  # fmt: skip


@dataclass(frozen=True)
class ContinueEntry:
    work_id: str
    title: str
    unit_id: str
    number: str
    unit_title: str | None
    state: Literal["in_progress", "next"]
    target: Target
    approximate: bool
    # what the reader opens, the very pair of the manifest of L06 (`/lire/<type>/<id>`); only when
    # `target` is `kept_copy`
    carrier_type: Literal["content"] | None = None
    carrier_id: str | None = None
    # the address « Ouvrir dans <kind> » opens, when `target` names a destination: the target of
    # the destination that answered, the one this profile has an account on — never another's
    open_url: str | None = None
    # the id of that target: two servers of one kind are two destinations, each entry opens its own
    target_id: str | None = None
    # where she is in this Unit (`in_progress` only: the next Unit is not begun), and the device
    # and the date of the last reading of the series (the Unit last touched), when known
    percent: float | None = None
    device: str | None = None
    last_read_at: datetime | None = None


def _order(row: Any) -> tuple[bool, float, int, int, str]:
    """Reading order of the Units of an Edition (the order of the series bundle)."""
    return (
        row.number_sort is None,
        row.number_sort or 0.0,
        row.number_segment_ordinal,
        row.source_order,
        row.id,
    )


class ContinueReading:
    def __init__(self, engine: sa.Engine, access: ContentAccess) -> None:
        self._engine = engine
        self._publication = PublicationService(engine, access)
        self._access = access

    def entries(self, user_id: str, limit: int = DEFAULT_ENTRIES) -> list[ContinueEntry]:
        limit = max(0, min(limit, MAX_ENTRIES))
        found: list[ContinueEntry] = []
        if limit == 0:
            return found
        with self._engine.connect() as connection:
            scope = reader_scope(connection, user_id)
            seen = 0
            while len(found) < limit and seen < MAX_WORKS_EXAMINED:
                works = self._works_under_way(connection, user_id, scope, seen, _BATCH)
                if not works:
                    break
                seen += len(works)
                for work_id in works:
                    entry = self._entry(connection, user_id, scope, work_id)
                    if entry is not None:
                        found.append(entry)
                        if len(found) == limit:
                            break
        return found

    def edition_progress(self, user_id: str, edition_id: str) -> EditionProgress | None:
        """What `user_id` has read of one Edition, if she may see it (else, as if nothing)."""
        with self._engine.connect() as connection:
            return edition_progress(connection, user_id, [edition_id]).get(edition_id)

    def edition_visible(self, user_id: str, edition_id: str) -> bool:
        """Whether the Edition is in what `user_id` may see (the filter of every surface)."""
        with self._engine.connect() as connection:
            scope = reader_scope(connection, user_id)
            return visible_edition_ids(connection, scope, [edition_id]) == {edition_id}

    # -- which series ---------------------------------------------------------------------------

    @staticmethod
    def _works_under_way(
        connection: sa.Connection, user_id: str, scope: object, offset: int, size: int
    ) -> list[str]:
        """The Works she has started, the most recently touched first, only those she may see."""
        progress, unit, edition = _PROGRESS, catalog.unit, catalog.edition
        position = reading.reading_position
        touched = sa.func.min(position.c.client_ts, position.c.server_ts)
        sibling = catalog.unit.alias("sibling")
        done = _PROGRESS.alias("done")  # not the outer row: the subquery must not correlate to it
        rows = connection.execute(
            sa.select(edition.c.work_id, sa.func.max(touched).label("ts"))
            .select_from(
                progress.join(unit, unit.c.id == progress.c.unit_id)
                .join(edition, edition.c.id == unit.c.edition_id)
                .join(position, position.c.id == progress.c.latest_position_id)
            )
            .where(
                progress.c.user_id == user_id,
                progress.c.state.in_(("in_progress", "read")),
                visible_editions_clause(scope),  # type: ignore[arg-type]
                sa.or_(
                    progress.c.state == "in_progress",
                    sa.exists().where(  # a later Unit that is not read yet: something to offer
                        sibling.c.edition_id == unit.c.edition_id,
                        sibling.c.track == "main",
                        sibling.c.id.not_in(
                            sa.select(done.c.unit_id).where(
                                done.c.user_id == user_id, done.c.state == "read"
                            )
                        ),
                    ),
                ),
            )
            .group_by(edition.c.work_id)
            .order_by(sa.desc("ts"), edition.c.work_id)
            .limit(size)
            .offset(offset)
        )
        return [str(r.work_id) for r in rows]

    # -- one series -----------------------------------------------------------------------------

    def _entry(
        self, connection: sa.Connection, user_id: str, scope: object, work_id: str
    ) -> ContinueEntry | None:
        progress, unit, edition = _PROGRESS, catalog.unit, catalog.edition
        position = reading.reading_position
        touched = sa.func.min(position.c.client_ts, position.c.server_ts)
        rows = connection.execute(
            sa.select(
                unit.c.id,
                unit.c.edition_id,
                unit.c.number_sort,
                unit.c.number_segment_ordinal,
                unit.c.source_order,
                progress.c.state,
                progress.c.approximate,
                touched.label("ts"),
                position.c.percent,
                position.c.device_id,
                position.c.client_ts,
                position.c.server_ts,
            )
            .select_from(
                progress.join(unit, unit.c.id == progress.c.unit_id)
                .join(edition, edition.c.id == unit.c.edition_id)
                .join(position, position.c.id == progress.c.latest_position_id)
            )
            .where(
                progress.c.user_id == user_id,
                progress.c.state.in_(("in_progress", "read")),
                edition.c.work_id == work_id,
                unit.c.track == "main",
                visible_editions_clause(scope),  # type: ignore[arg-type]
            )
        ).all()
        if not rows:
            return None
        under_way = [r for r in rows if r.state == "in_progress"]
        if under_way:
            for current in sorted(under_way, key=lambda r: (str(r.ts), _order(r)), reverse=True):
                made = self._described(
                    connection, user_id, str(current.id), "in_progress",
                    bool(current.approximate), current,
                )  # fmt: skip
                if made is not None:
                    return made  # the latest one that can still be opened
        latest = max(rows, key=lambda r: (str(r.ts), _order(r)))
        edition_id = str(latest.edition_id)
        furthest = max(_order(r) for r in rows if str(r.edition_id) == edition_id)
        later = connection.execute(
            sa.select(
                unit.c.id, unit.c.number_sort, unit.c.number_segment_ordinal, unit.c.source_order
            ).where(
                unit.c.edition_id == edition_id,
                unit.c.track == "main",
                unit.c.id.not_in(
                    sa.select(progress.c.unit_id).where(
                        progress.c.user_id == user_id, progress.c.state == "read"
                    )
                ),
            )
        ).all()
        ahead = sorted((r for r in later if _order(r) > furthest), key=_order)
        for candidate in ahead[:MAX_CANDIDATES]:
            made = self._described(connection, user_id, str(candidate.id), "next", False, latest)
            if made is not None:
                return made  # the first Unit after the furthest that can be opened
        return None

    def _described(
        self, connection: sa.Connection, user_id: str, unit_id: str,
        state: Literal["in_progress", "next"], approximate: bool, seen: Any,
    ) -> ContinueEntry | None:  # fmt: skip
        """`seen`: the row of the Unit last touched, which says the device and the date; its
        place in the Unit is the entry's only when that Unit is the one being read."""
        found = self._target(connection, user_id, unit_id)
        if found is None:
            return None
        target, carrier, elsewhere = found
        row = connection.execute(
            sa.select(
                catalog.unit.c.number_raw,
                catalog.unit.c.title,
                catalog.work.c.id,
                catalog.work.c.title.label("work_title"),
            )
            .select_from(
                catalog.unit.join(
                    catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id
                ).join(catalog.work, catalog.work.c.id == catalog.edition.c.work_id)
            )
            .where(catalog.unit.c.id == unit_id)
        ).one()
        return ContinueEntry(
            work_id=str(row.id), title=str(row.work_title), unit_id=unit_id,
            number=str(row.number_raw), unit_title=row.title, state=state, target=target,
            approximate=approximate,
            carrier_type="content" if carrier else None, carrier_id=carrier,
            open_url=elsewhere.url if elsewhere else None,
            target_id=elsewhere.target_id if elsewhere else None,
            percent=None if state == "next" or seen.percent is None else float(seen.percent),
            device=seen.device_id, last_read_at=min(seen.client_ts, seen.server_ts),
        )  # fmt: skip

    def _target(
        self, connection: sa.Connection, user_id: str, unit_id: str
    ) -> tuple[Target, str | None, Elsewhere | None] | None:
        """What the entry opens, judged by the one query (J5-S2): a kept copy of hers that is not
        read elsewhere opens the reader; a Unit read elsewhere names the kind of the destination
        that answered — on each content of the Unit first, then through its coverage (an assembled
        volume is an `export_item` with no content of its own: it is read elsewhere all the same).
        The answer is the destination itself: the address of the target the profile's account is
        on, and that target's id — never one of another's, and never another server of the same
        kind."""
        try:
            carrier = self._publication.choose(user_id, "unit", unit_id)
            if self._access.resolve(user_id, *carrier).full_allowed:
                return "kept_copy", carrier[1], None  # the manifest's own `content` pair
        except (NoReadableCopyError, UnknownCarrierError):
            pass
        elsewhere: Elsewhere | None = None
        contents = connection.execute(
            sa.select(storage.content.c.id)
            .where(storage.content.c.unit_id == unit_id)
            .order_by(storage.content.c.id)
        ).scalars()
        for content_id in contents:
            found = read_elsewhere(connection, user_id=user_id, content_id=str(content_id))
            if found is not None:
                elsewhere = elsewhere or found  # the first answer, whatever the content
                continue
            try:
                self._access.resolve(user_id, "content", str(content_id))
            except (NoReadableCopyError, UnknownCarrierError):
                continue  # not hers to open: another content of the Unit may be
            return "kept_copy", str(content_id), None
        if elsewhere is None:  # no content of the Unit said it: the coverage may still hold one
            elsewhere = read_elsewhere_of_unit(connection, user_id=user_id, unit_id=unit_id)
        return (elsewhere.kind, None, elsewhere) if elsewhere else None
