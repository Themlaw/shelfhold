"""Confirming that Kavita indexed what Shelfhold wrote: polling (EF-EXP-034, M1a).

Run by the export job after the targeted scan. Each written record is looked for in the API of
Kavita (its series, then the files of that series). At half the delay a second scan is asked; at
the delay the record is left `À vérifier` with `target.not_indexed` and the probable causes, and
the destination waits for the admin (`jobs retry` once fixed). Nothing is exported again.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.application.target_state import ensure_active, save_health
from shelfhold.export.domain.kavita_arrival import DEFAULT_DELAY, Step, next_step, probable_causes
from shelfhold.export.domain.path_map import PathMapError
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.reinstall import library_type_of
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.errors import RootBlockedError
from shelfhold.infrastructure.schema import export

__all__ = ["ArrivalPoller", "ArrivalReport"]

NOT_INDEXED = "target.not_indexed"


@dataclass
class ArrivalReport:
    indexed: list[str] = field(default_factory=list)  # relative paths
    rescanned: list[str] = field(default_factory=list)  # folders asked for a second scan
    not_indexed: list[str] = field(default_factory=list)  # relative paths, newly declared


class ArrivalPoller:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        events: TargetEvents,
        *,
        delay: timedelta = DEFAULT_DELAY,
        max_scans: int = 2,
        fsops: Callable[[], Fsops] | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events
        self._delay = delay
        self._max_scans = max_scans  # 2; 1 when the second scan is cut
        self._fsops = fsops  # the sentinel's word before a scan (`require_scan_allowed`)

    def _scan_allowed(self, ctx: DestinationContext, remote_folder: str) -> bool:
        """A folder under a root whose sentinel is missing or wrong is not scanned: Kavita would
        read an empty folder as a deletion. Without the means to tell, nothing is scanned."""
        if self._fsops is None:
            return True
        try:
            self._fsops().require_scan_allowed(ctx.path_map.to_local(remote_folder))
        except (RootBlockedError, PathMapError, OSError, LookupError, RuntimeError):
            return False
        return True

    def waiting_destinations(self, target_id: str, library_id: int) -> list[str]:
        """The destinations of the target `target_id`, in its remote library `library_id`, that
        hold a written record Kavita has not been seen to index yet: the only ones an event of that
        library is worth a look for (EF-EXP-034). Read from the database, no request."""
        t, destination, library, link = (
            export.export_record, export.shelf_destination, export.remote_library,
            export.shelf_destination_remote_library,
        )  # fmt: skip
        with self._engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(destination.c.id)
                    .select_from(
                        destination.join(
                            link, link.c.shelf_destination_id == destination.c.id
                        ).join(library, library.c.id == link.c.remote_library_id)
                    )
                    .where(
                        destination.c.target_id == target_id,
                        library.c.export_target_id == target_id,
                        library.c.remote_ref == str(library_id),
                        sa.exists().where(
                            t.c.shelf_destination_id == destination.c.id,
                            t.c.locator_type == "path",
                            t.c.state.in_(("written", "scanned")),
                            sa.or_(t.c.error_code.is_(None), t.c.error_code != NOT_INDEXED),
                        ),
                    )
                    .order_by(destination.c.id)
                ).scalars()
            )

    async def poll(self, client: KavitaClient, destination_id: str) -> ArrivalReport:
        report = ArrivalReport()
        with self._engine.connect() as connection:
            ensure_active(connection, destination_id)  # no scan for a paused destination
            ctx = load_context(connection, destination_id)
            records = self._pending(connection, destination_id)
            ref = connection.execute(
                sa.select(export.remote_library.c.remote_ref).where(
                    export.remote_library.c.id == ctx.remote_library_id
                )
            ).scalar()
            expected_type = connection.execute(
                sa.select(export.shelf_destination.c.kavita_library_type).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar()
        if not records or ref is None or ctx.root_local is None:
            return report
        library_id = int(ref)
        series = await client.series_of_library(library_id)
        files: dict[int, set[str]] = {}
        rescanned_folders: set[str] = set()
        outcomes: list[tuple[_Pending, Step, int | None, str]] = []
        for record in records:
            remote = ctx.path_map.to_remote(ctx.local(record.rel_path))
            folder = posixpath.dirname(remote)
            found = next((s for s in series if s.lives_in(folder)), None)
            indexed = False
            if found is not None:
                if found.id not in files:
                    files[found.id] = set(await client.series_files(found.id))
                indexed = remote in files[found.id]
            step = next_step(
                written_at=record.written_at, now=self._clock.now(), indexed=indexed,
                rescanned=record.state == "scanned", delay=self._delay,
                max_scans=self._max_scans,
            )  # fmt: skip
            outcomes.append((record, step, found.id if found else None, remote))
        causes = await self._causes(client, library_id, expected_type, outcomes)
        for record, step, series_id, remote in outcomes:
            folder = posixpath.dirname(remote)
            if step is Step.RESCAN and not self._scan_allowed(ctx, folder):
                continue  # the record stays as it is: the next poll asks again
            if step is Step.RESCAN and folder not in rescanned_folders:
                rescanned_folders.add(folder)
                await client.scan_folder(folder)
                report.rescanned.append(folder)
            self._apply(ctx, record, step, series_id, remote, causes, report)
        self._save_waiting(destination_id, client)
        return report

    # -- the database --------------------------------------------------------------------------

    def _pending(self, connection: sa.Connection, destination_id: str) -> list[_Pending]:
        t = export.export_record
        rows = connection.execute(
            sa.select(t)
            .where(
                t.c.shelf_destination_id == destination_id,
                t.c.locator_type == "path",
                t.c.state.in_(("written", "scanned")),
                # declared `not_indexed`: left alone until `jobs retry` clears the error
                sa.or_(t.c.error_code.is_(None), t.c.error_code != NOT_INDEXED),
            )
            .order_by(t.c.rel_path)
        )
        return [
            _Pending(
                r.export_item_id,
                r.rel_path,
                r.state,
                r.error_code == NOT_INDEXED,
                r.written_by_shelfhold_at or r.exported_at or r.created_at,
            )
            for r in rows
        ]

    def _apply(
        self,
        ctx: DestinationContext,
        record: _Pending,
        step: Step,
        series_id: int | None,
        remote: str,
        causes: tuple[str, ...],
        report: ArrivalReport,
    ) -> None:
        t = export.export_record
        where = (
            t.c.shelf_destination_id == ctx.destination_id,
            t.c.export_item_id == record.item_id,
        )
        with write_transaction(self._engine) as connection:
            if step is Step.INDEXED:
                connection.execute(
                    sa.update(t)
                    .where(*where)
                    .values(
                        state="resolved",
                        remote_ref=str(series_id),
                        error_code=None,
                        resolved_at=self._clock.now(),  # the moment the scan was seen to hold it
                    )
                )
                report.indexed.append(record.rel_path)
            elif step is Step.RESCAN:
                connection.execute(sa.update(t).where(*where).values(state="scanned"))
            elif step is Step.NOT_INDEXED and not record.declared:
                connection.execute(sa.update(t).where(*where).values(error_code=NOT_INDEXED))
                report.not_indexed.append(record.rel_path)
                self._events.emit(
                    NOT_INDEXED, destination_id=ctx.destination_id, rel_path=record.rel_path,
                    remote_path=remote, causes=list(causes),
                )  # fmt: skip

    def _save_waiting(self, destination_id: str, client: KavitaClient) -> None:
        t = export.export_record
        with write_transaction(self._engine) as connection:
            target_id = connection.execute(
                sa.select(export.shelf_destination.c.target_id).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar_one()
            left = connection.execute(
                sa.select(sa.func.count())
                .select_from(t)
                .join(
                    export.shelf_destination,
                    export.shelf_destination.c.id == t.c.shelf_destination_id,
                )
                .where(
                    export.shelf_destination.c.target_id == target_id, t.c.error_code == NOT_INDEXED
                )
            ).scalar_one()
            health = client.health
            health.not_indexed = left > 0
            health.refresh_waiting()
            save_health(connection, target_id, health)

    async def _causes(
        self,
        client: KavitaClient,
        library_id: int,
        expected_type: str | None,
        outcomes: list[tuple[_Pending, Step, int | None, str]],
    ) -> tuple[str, ...]:
        late = [remote for r, step, _, remote in outcomes if step is Step.NOT_INDEXED]
        if not late:
            return ()
        library = next(
            (lib for lib in await client.list_libraries() if lib.get("id") == library_id), None
        )
        expected = int(library_type_of(expected_type)) if expected_type else None
        found: list[str] = []
        for cause in (probable_causes(library, remote, expected) for remote in late):
            found.extend(c for c in cause if c not in found)
        return tuple(found)


@dataclass(frozen=True)
class _Pending:
    item_id: str
    rel_path: str
    state: str
    declared: bool  # `target.not_indexed` was already announced for this record
    written_at: datetime
