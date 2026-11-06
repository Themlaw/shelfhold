"""The cover of a series, posted by the API from its chapter 1 (EF-EXP-045, M1a).

Run by the export job once Kavita has indexed the series (`ArrivalPoller`): the series exists in
Kavita from the moment one of its files is `resolved`. The cover is the first page of the lowest
chapter from 1 on (chapter 0 and the specials never are it); it is posted once, as bare base64
(no `data:` prefix), and what was posted is kept in the `field_owner` registry so that the next
run posts nothing, until another chapter becomes the lowest. Whether a cover is due is read from
the database alone; the archive is read from the disk only for a cover that is due.

A cover is posted with a lock, so a cover someone else chose is never covered over: the lock of
the series is read first and `FieldGuard.decide` answers (EF-EXP-057). A lock Shelfhold does not
hold is a third party's and the cover is left alone, whichever chapter arrives later. A page that
cannot be read (archive gone, damaged, no image) is remembered for that chapter, so the next runs
do not open it again until another chapter becomes the cover.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.export.application.field_guard import FieldGuard
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.target_state import FieldRegistry
from shelfhold.export.domain.field_owner import FieldAction
from shelfhold.export.domain.naming import FrozenAttrs, series_cover_source
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust import check_cbz_entries
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import export_sources

__all__ = ["COVER_FIELD", "CoverTask", "SeriesCovers"]

log = structlog.get_logger(__name__)
COVER_FIELD = "cover"
_WRITTEN = ("written", "scanned", "resolved")
_UNREADABLE = "unreadable"  # kept in the registry for a chapter whose page cannot be read


@dataclass(frozen=True)
class CoverTask:
    export_series_id: str
    kavita_series_id: int
    unit_id: str
    content_hash: str


class SeriesCovers:
    def __init__(
        self, engine: Engine, clock: Clock, fsops: Callable[[], Fsops], events: TargetEvents
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events
        self._fsops = fsops  # asked for only when a cover is due

    def due(self, destination_id: str) -> list[CoverTask]:
        """The covers to post, from the database alone."""
        record, item, coverage = export.export_record, export.export_item, export.unit_coverage
        link = export.edition_export_series
        with self._engine.connect() as connection:
            library = load_context(connection, destination_id).remote_library_id
            if library is None:
                return []
            rows = connection.execute(
                sa.select(
                    link.c.export_series_id,
                    record.c.state,
                    record.c.remote_ref,
                    record.c.content_hash,
                    item.c.frozen_attrs,
                    coverage.c.unit_id,
                )
                .select_from(
                    record.join(item, item.c.id == record.c.export_item_id)
                    .join(
                        coverage,
                        sa.and_(
                            coverage.c.carrier_type == "export_item",
                            coverage.c.carrier_id == item.c.id,
                        ),
                    )
                    .join(
                        link,
                        sa.and_(
                            link.c.edition_id == item.c.edition_id,
                            link.c.remote_library_id == library,
                        ),
                    )
                )
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.locator_type == "path",
                    record.c.state.in_(_WRITTEN),
                )
                .order_by(record.c.rel_path)
            ).all()
            registry = FieldRegistry(connection)
            by_series: dict[str, list[Any]] = {}
            for row in rows:
                by_series.setdefault(row.export_series_id, []).append(row)
            tasks: list[CoverTask] = []
            for series_id, series_rows in by_series.items():
                known = [
                    int(r.remote_ref)
                    for r in series_rows
                    if r.state == "resolved" and str(r.remote_ref).isdigit()
                ]
                if not known:
                    continue  # Kavita has not indexed this series yet
                frozen = [FrozenAttrs(**r.frozen_attrs) for r in series_rows]
                chosen = series_cover_source(frozen)
                if chosen is None:
                    continue
                source = next(r for r, f in zip(series_rows, frozen, strict=True) if f is chosen)
                task = CoverTask(series_id, known[0], source.unit_id, source.content_hash or "")
                owned = registry.get(destination_id, series_id, COVER_FIELD)
                settled = _value(task)
                if (
                    owned is not None
                    and owned.pushed
                    and owned.last_value in (settled, {**settled, _UNREADABLE: True})
                ):
                    continue  # posted, or known unreadable: nothing to look at
                tasks.append(task)
        return tasks

    async def publish(self, client: KavitaClient, destination_id: str) -> list[int]:
        """Post the covers that are due and that nobody else holds; the Kavita series ids that
        got one."""
        tasks = self.due(destination_id)
        if not tasks:
            return []
        try:
            current = await client.series_by_ids([t.kavita_series_id for t in tasks])
        except TargetError as error:
            log.info("export.cover_failed", destination_id=destination_id,
                     reason=type(error).__name__)  # fmt: skip
            return []
        fsops: Fsops | None = None
        posted: list[int] = []
        for task in tasks:
            series = current.get(task.kavita_series_id)
            if series is None:
                continue
            with self._engine.begin() as connection:
                owned = FieldRegistry(connection).get(
                    destination_id, task.export_series_id, COVER_FIELD
                )
                claim = owned
                if owned is not None and _UNREADABLE in (owned.last_value or {}):
                    claim = None  # a note about a page, not a cover Shelfhold posted
                action = FieldGuard(FieldRegistry(connection), self._events).decide(
                    destination_id, task.export_series_id, COVER_FIELD,
                    locked_in_kavita=series.cover_locked,
                    kavita_value=None if claim is None else claim.last_value,
                )  # fmt: skip
            if action is FieldAction.LEAVE:
                continue  # a cover someone else chose: never covered over
            fsops = fsops or self._fsops()
            try:
                image = self._first_page(fsops, task.unit_id)
            except (OSError, UnsafeArchiveError, ValueError, KeyError) as error:
                log.info("export.cover_unreadable", unit_id=task.unit_id,
                         reason=type(error).__name__)  # fmt: skip
                image = None
            if image is None:
                self._record(destination_id, task, {**_value(task), _UNREADABLE: True})
                continue
            try:
                await client.upload_series_cover(task.kavita_series_id, image)
            except TargetError as error:  # Kavita unreachable: the next run posts it again
                log.info("export.cover_failed", destination_id=destination_id,
                         reason=type(error).__name__)  # fmt: skip
                break
            self._record(destination_id, task, _value(task))
            posted.append(task.kavita_series_id)
        return posted

    def _record(self, destination_id: str, task: CoverTask, value: dict[str, object]) -> None:
        with self._engine.begin() as connection:
            FieldRegistry(connection).record_push(
                destination_id, task.export_series_id, COVER_FIELD, value, self._clock.now()
            )

    def _first_page(self, fsops: Fsops, unit_id: str) -> bytes | None:
        with self._engine.connect() as connection:
            source = export_sources(connection, [unit_id]).get(unit_id)
        if source is None or source.local_path is None:
            return None
        with (
            fsops.open_read(source.local_path) as stream,
            safe_open_archive(stream, name=PurePath(source.local_path).name) as archive,
        ):
            pages = check_cbz_entries(archive).images
            return bytes(archive.read(pages[0])) if pages else None


def _value(task: CoverTask) -> dict[str, Any]:
    return {"unit_id": task.unit_id, "content_hash": task.content_hash}
