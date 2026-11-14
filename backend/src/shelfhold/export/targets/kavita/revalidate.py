"""The names revalidated after a major update of Kavita (EF-EXP-049).

When the version line of Kavita changes (`0.9.1` to `0.9.2`: the first three numbers, the patch
number does not count), a job replays `plugin/parse` through `parse-bulk` (100 names at most per
call) on a sample of the manifest. The names the new parser would misread, the ones the export
guard of the plan would refuse, are reported: `reconcile.drift` (`kind = kavita_major_update`) and
the list kept in the health of the target. Nothing is renamed, moved or written: the paths are
frozen (I22), and a name that now reads wrong is for the admin to decide.

The replay is owed until it has run: a Kavita that does not answer, or a `parse-bulk` that fails,
leaves the version of the last replay as it was, and the next daily run tries again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import load_health, save_health
from shelfhold.export.domain.kavita_compat import KavitaVersion
from shelfhold.export.domain.naming import (
    ExportPath,
    FrozenAttrs,
    NamingGuardError,
    check_parsed_path,
    parsed_components,
)
from shelfhold.export.targets.kavita.adopt import BulkParse, library_type_named
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.dto import LibraryType
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export

__all__ = ["Rejected", "RevalidationReport", "Revalidator"]

SAMPLE_RECORDS: Final = 100  # records replayed at most: ~200 names, three calls of `parse-bulk`
KEPT_REJECTED: Final = 100  # paths kept in the health of the target
UNANSWERED: Final = "kavita.parse_unanswered"  # `parse-bulk` gave no answer for a name


@dataclass(frozen=True)
class Rejected:
    """A written path the replayed parser would refuse, with the code the plan would give."""

    rel_path: str
    code: str


@dataclass(frozen=True)
class RevalidationReport:
    replayed: bool = False
    version: str | None = None
    sampled: int = 0
    rejected: tuple[Rejected, ...] = ()


@dataclass(frozen=True)
class _Record:
    rel_path: str
    frozen: FrozenAttrs
    library_type: LibraryType

    @property
    def extension(self) -> str:
        return PurePosixPath(self.rel_path).suffix

    @property
    def path(self) -> ExportPath:
        *folders, name = self.rel_path.split("/")
        return ExportPath(tuple(folders), name)

    @property
    def components(self) -> list[str]:
        return parsed_components(self.frozen, self.path, self.extension)


def _line(raw: str | None) -> tuple[int, ...] | str | None:
    version = KavitaVersion.parse(raw)
    return version.parts[:3] if version else raw


class Revalidator:
    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    async def check(self, client: KavitaClient, target_id: str) -> RevalidationReport:
        """The daily check: replay once per version line, report, write nothing else.

        A failure of Kavita propagates (`TargetError`) before anything is recorded.
        """
        await client.probe()
        current = client.health.version
        if current is None:  # no version read: nothing to compare, nothing to record
            return RevalidationReport()
        with self._engine.connect() as connection:
            baseline = load_health(connection, target_id).replayed_version
        if baseline is None or _line(baseline) == _line(current):
            if baseline is None:  # first sight of this Kavita: its parser is the reference
                self._record(target_id, current, None)
            return RevalidationReport(version=current)
        report = await self.replay_sample(client, target_id, kind="kavita_major_update")
        self._record(target_id, current, [r.rel_path for r in report.rejected][:KEPT_REJECTED])
        return RevalidationReport(
            replayed=True, version=current, sampled=report.sampled, rejected=report.rejected
        )

    async def replay_sample(
        self,
        client: KavitaClient,
        target_id: str,
        *,
        destination_id: str | None = None,
        library_type: LibraryType | None = None,
        kind: str | None = None,
    ) -> RevalidationReport:
        """Replay `parse-bulk` on a sample of the manifest (of one destination, if given), with
        the type of the library of each record or the one given. With `kind`, the names refused
        are announced as `reconcile.drift`. Nothing is written."""
        records = self._sample(target_id, destination_id, library_type)
        rejected: list[Rejected] = []
        for kind_of_library in sorted({r.library_type for r in records}):
            group = [r for r in records if r.library_type is kind_of_library]
            names = sorted({name for r in group for name in r.components})
            bulk = await BulkParse.fetch(client, names, kind_of_library)
            for record in group:
                code = _refusal(record, bulk)
                if code is not None:
                    rejected.append(Rejected(record.rel_path, code))
        rejected.sort(key=lambda r: r.rel_path)
        if kind is not None and rejected:
            self._events.emit(
                "reconcile.drift", target_id=target_id, kind=kind, count=len(rejected),
                sample_size=len(records),
            )  # fmt: skip
        return RevalidationReport(replayed=True, sampled=len(records), rejected=tuple(rejected))

    def _record(self, target_id: str, version: str, rejected: list[str] | None) -> None:
        with write_transaction(self._engine) as connection:
            health = load_health(connection, target_id)
            health.replayed_version = version
            if rejected is not None:
                health.replay_rejected = rejected
            save_health(connection, target_id, health)

    def _sample(
        self, target_id: str, destination_id: str | None, library_type: LibraryType | None
    ) -> list[_Record]:
        """At most `SAMPLE_RECORDS` records, spread over the sorted manifest (the same ones on
        every run, so two replays of the same version ask the same names)."""
        record, item, dest = export.export_record, export.export_item, export.shelf_destination
        query = (
            sa.select(record.c.rel_path, item.c.frozen_attrs, dest.c.kavita_library_type)
            .select_from(
                record.join(item, item.c.id == record.c.export_item_id).join(
                    dest, dest.c.id == record.c.shelf_destination_id
                )
            )
            .where(dest.c.target_id == target_id, record.c.rel_path.is_not(None))
            .order_by(record.c.rel_path, record.c.export_item_id)
        )
        if destination_id is not None:
            query = query.where(dest.c.id == destination_id)
        with self._engine.connect() as connection:
            rows = connection.execute(query).all()
        step = max(1, -(-len(rows) // SAMPLE_RECORDS))
        found: list[_Record] = []
        for row in rows[::step]:
            try:
                frozen = FrozenAttrs.stored(row.frozen_attrs)
            except ValueError:  # an item that names nothing readable: nothing to replay
                continue
            found.append(
                _Record(
                    row.rel_path,
                    frozen,
                    library_type or library_type_named(row.kavita_library_type),
                )
            )
        return found


def _refusal(record: _Record, bulk: BulkParse) -> str | None:
    try:
        check_parsed_path(record.frozen, record.path, bulk, record.extension)
    except NamingGuardError as error:
        return error.code
    except KeyError:  # `parse-bulk` did not answer this name: never taken for a good reading
        return UNANSWERED
    except ValueError:  # a number that is no plain decimal
        return "export.name_misread_number"
    return None
