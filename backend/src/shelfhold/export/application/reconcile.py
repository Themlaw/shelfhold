"""Foreign writer detection and the reduced reconcile of a tree destination (EF-EXP-043, 018).

One pass per shelf destination: observe the managed series folders, compare with the manifest,
decide with `export.domain.reconcile`, then record. Shelfhold never deletes, overwrites or moves
what a third party did: a file it did not write is reported (or pauses the series), a file that
vanished is put back once, asked about, or only reported (an adopted one). Paths covered by an
open intent are skipped. Too many missing files at once (an absent mount, an unplugged disk) block
the whole pass before anything is decided.
"""

import time
from collections import defaultdict
from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application.intents import open_intent_paths
from shelfhold.export.application.locks import LockBusyError, ResourceLocks
from shelfhold.export.application.preflight import (
    DENIED,
    DestinationNotWritableError,
    PermissionPreflight,
)
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.waiting_marks import clear_mount_blocked, mark_mount_blocked
from shelfhold.export.domain.physical_roots import RESERVED_DIR, SENTINEL_FILE, RootState
from shelfhold.export.domain.reconcile import (
    LIVE_STATES,
    REEXPORT_ATTEMPTED,
    REEXPORT_WINDOW,
    Change,
    Disappeared,
    ManagedFile,
    Seen,
    Thresholds,
    classify_change,
    classify_missing,
    missing_blocks,
    thresholds_from,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import elapsed_ms
from shelfhold.infrastructure.schema import export, platform
from shelfhold.library.public import (
    destination_identities,
    export_sources,
    refresh_destination_identity,
)

__all__ = ["ReconcileReport", "Reconciler"]

FOREIGN_WRITER = "foreign_writer"
RESERVED_NAMES = frozenset({SENTINEL_FILE, RESERVED_DIR})
MAX_DEPTH = 8

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReconcileReport:
    blocked: str | None = None  # `missing_ratio` or `sentinel`: nothing was decided
    reexport: tuple[str, ...] = ()  # export item ids put back to `planned`
    missing: tuple[str, ...] = ()  # asked of the admin (state `missing`)
    adopted_missing: tuple[str, ...] = ()  # reported only
    unknown: tuple[str, ...] = ()  # folders of the root that the manifest knows nothing about
    paused: tuple[str, ...] = ()  # export series ids paused by this pass
    suspect: tuple[str, ...] = ()  # mtime changed outside the window: the hash waits for it
    busy: tuple[str, ...] = ()  # folders skipped because an export holds them


def _found_something(report: ReconcileReport) -> bool:
    return any(bool(value) for value in vars(report).values())


class Reconciler:
    def __init__(self, engine: Engine, fsops: Fsops, *, clock: Clock, bus: EventBus) -> None:
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._bus = bus
        self._locks = ResourceLocks(engine, clock=clock)
        self._blocked: dict[str, str] = {}  # destination -> reason already announced

    # -- one pass --------------------------------------------------------------------------------

    def reconcile(self, destination_id: str, *, in_window: bool = False) -> ReconcileReport:
        """`in_window`: the hash may be read (maintenance window, ENF-OPS-014); outside it only
        the size and the mtime are compared."""
        started = time.perf_counter()
        report = self._reconcile(destination_id, in_window=in_window)
        counts = {
            name: len(value) if isinstance(value, tuple) else value
            for name, value in vars(report).items()
        }  # names of folders and files are never logged: the counts only
        (log.info if _found_something(report) else log.debug)(
            "reconcile.finished", destination_id=destination_id, in_window=in_window,
            duration_ms=elapsed_ms(started), **counts,
        )  # fmt: skip
        return report

    def _reconcile(self, destination_id: str, *, in_window: bool) -> ReconcileReport:
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            if ctx.transport != "tree" or ctx.root_local is None:
                return ReconcileReport()
            managed = self._managed(connection, destination_id)
            covered = open_intent_paths(connection)
        root = ctx.root_local
        sentinel_ok = self._fsops.status(root).state is RootState.OK
        if not sentinel_ok:
            self._announce_block(destination_id, "sentinel")
            return ReconcileReport(blocked="sentinel")

        def local(rel: str) -> str:
            return f"{root}/{rel}"

        def skipped(rel: str) -> bool:
            path = local(rel)
            return any(path == c or path.startswith(c.rstrip("/") + "/") for c in covered)

        live = {
            f.rel_path: f for f in managed if f.state in LIVE_STATES and not skipped(f.rel_path)
        }
        seen = {rel: self._fsops.observe(local(rel)) for rel in live}
        gone = [live[rel] for rel, found in seen.items() if found is None]
        limits = self._thresholds()
        if missing_blocks(
            total=len(live), missing=len(gone), floor=limits.floor, ratio=limits.ratio,
            ceiling=limits.ceiling,
        ):  # fmt: skip
            self._announce_block(destination_id, "missing_ratio")
            return ReconcileReport(blocked="missing_ratio")

        self._blocked.pop(destination_id, None)  # the state changed: a new block is news again
        clear_mount_blocked(self._engine, destination_id)
        folders = sorted({f.rel_path.split("/", 1)[0] for f in managed})
        known = {f.rel_path for f in managed}
        foreign: dict[str, str] = {}  # folder -> why
        suspect: list[str] = []
        now = self._clock.now()
        refresh: list[tuple[str, int, int, int, int]] = []
        settled: list[str] = []
        for rel, found in seen.items():
            if found is None:
                continue
            kind, identity = found
            managed_file = live[rel]
            dev = self._fsops.device_of(local(rel)) if kind == "file" else None
            change = classify_change(
                managed_file,
                Seen(
                    identity.size, identity.mtime_ns, regular=kind == "file", dev=dev,
                    ino=identity.ino,
                ),
            )  # fmt: skip
            folder = rel.split("/", 1)[0]
            if change is Change.SIZE_OR_KIND:
                foreign.setdefault(folder, "modified")
            elif change is Change.MTIME_ONLY:
                if not in_window:
                    suspect.append(managed_file.item_id)
                elif self._fsops.sha256_of(local(rel)) == managed_file.file_hash:
                    refresh.append(
                        (
                            managed_file.item_id,
                            identity.size,
                            identity.mtime_ns,
                            dev or 0,
                            identity.ino,
                        )
                    )
                else:
                    foreign.setdefault(folder, "modified")
            elif (
                managed_file.reexport_attempted
                and managed_file.written_at is not None
                and now - managed_file.written_at >= REEXPORT_WINDOW
            ):
                settled.append(managed_file.item_id)  # it stayed: the loop suspicion is over
        unknown_files: list[str] = []
        for folder in folders:
            for rel in self._files_under(local(folder), folder):
                if rel in known or skipped(rel):
                    continue
                unknown_files.append(rel)
                foreign.setdefault(folder, "unknown_file")
        for item in managed:
            # a reserved line (`planned`) whose intent is closed holds nothing of ours: a file
            # there was put by someone else (an abandoned intent, a write that never was)
            if (
                item.state == "planned"
                and not skipped(item.rel_path)
                and self._fsops.observe(local(item.rel_path)) is not None
            ):
                unknown_files.append(item.rel_path)
                foreign.setdefault(item.rel_path.split("/", 1)[0], "unknown_file")
        unknown = tuple(
            sorted(
                name
                for name in self._entries(root)
                if name not in RESERVED_NAMES and name not in folders and not skipped(name)
            )
        )

        decisions: dict[Disappeared, list[ManagedFile]] = defaultdict(list)
        available = self._sources_available(gone)
        for item in gone:
            decisions[
                classify_missing(
                    item, now=now, source_available=item.unit_id in available, sentinel_ok=True
                )
            ].append(item)
        for item in decisions[Disappeared.FOREIGN]:
            foreign.setdefault(item.rel_path.split("/", 1)[0], "deleted")

        log.debug(
            "reconcile.decided",
            managed=len(managed),
            live=len(live),
            gone=len(gone),
            foreign_folders=len(foreign),
            suspect=len(suspect),
            to_refresh=len(refresh),
            **{f"missing_{kind.name.lower()}": len(items) for kind, items in decisions.items()},
        )
        busy: list[str] = []
        paused: list[str] = []
        job_id = new_ulid()
        for folder in sorted(set(foreign) | {f.rel_path.split("/", 1)[0] for f in gone}):
            try:
                with self._locks.hold(
                    job_id, work=f"reconcile-{destination_id}", series=f"{destination_id}/{folder}"
                ):
                    paused += self._settle_folder(
                        destination_id, folder, managed, foreign, decisions, refresh, settled
                    )
            except LockBusyError:
                busy.append(folder)
        self._refresh(refresh, settled)
        if unknown or unknown_files:
            self._announce(destination_id, "reconcile.drift", kind="unknown_file")
        if decisions[Disappeared.REPORT_ONLY]:
            self._announce(
                destination_id, "reconcile.drift", kind="adopted_missing",
                count=len(decisions[Disappeared.REPORT_ONLY]),
            )  # fmt: skip
        return ReconcileReport(
            reexport=tuple(i.item_id for i in decisions[Disappeared.REEXPORT]),
            missing=tuple(i.item_id for i in decisions[Disappeared.ASK_ADMIN]),
            adopted_missing=tuple(i.item_id for i in decisions[Disappeared.REPORT_ONLY]),
            unknown=unknown, paused=tuple(paused), suspect=tuple(suspect), busy=tuple(busy),
        )  # fmt: skip

    # -- recording -------------------------------------------------------------------------------

    def _settle_folder(
        self,
        destination_id: str,
        folder: str,
        managed: list[ManagedFile],
        foreign: dict[str, str],
        decisions: dict[Disappeared, list[ManagedFile]],
        refresh: list[tuple[str, int, int, int, int]],
        settled: list[str],
    ) -> list[str]:
        record = export.export_record
        mine = {f.item_id for f in managed if f.rel_path.split("/", 1)[0] == folder}
        paused: list[str] = []
        with self._engine.begin() as connection:
            where = (record.c.shelf_destination_id == destination_id,)
            for item in decisions[Disappeared.REEXPORT]:
                if item.item_id in mine:
                    connection.execute(
                        sa.update(record)
                        .where(*where, record.c.export_item_id == item.item_id)
                        .values(state="planned", error_code=REEXPORT_ATTEMPTED)
                    )
            for item in decisions[Disappeared.FOREIGN] + decisions[Disappeared.ASK_ADMIN]:
                if item.item_id in mine:
                    connection.execute(
                        sa.update(record)
                        .where(*where, record.c.export_item_id == item.item_id)
                        .values(state="missing", error_code=None)
                    )
            if folder in foreign:
                for series_id in self._series_of(connection, destination_id, mine):
                    if self._pause(connection, destination_id, series_id):
                        paused.append(series_id)
        return paused

    def _pause(self, connection: Connection, destination_id: str, series_id: str) -> bool:
        series = export.export_series
        changed = connection.execute(
            sa.update(series)
            .where(series.c.id == series_id, series.c.paused_reason.is_(None))
            .values(paused_reason=FOREIGN_WRITER)
        ).rowcount
        if changed:  # announced once per episode, not at every pass
            self._bus.publish(
                connection, "target.foreign_writer_detected",
                {"destination_id": destination_id, "export_series_id": series_id},
            )  # fmt: skip
        return bool(changed)

    def _entries(self, local: str) -> dict[str, str]:
        """A folder that is gone is an empty one: its files take the `disappeared` road."""
        try:
            return dict(self._fsops.list_dir(local))
        except (FileNotFoundError, NotADirectoryError):
            return {}

    def _files_under(self, local: str, rel: str, depth: int = 0) -> list[str]:
        """Everything that is not a folder, below a managed series folder (`Specials/` and the
        other sub-folders of the naming are managed ones: they are walked, not flagged)."""
        found: list[str] = []
        for name, kind in sorted(self._entries(local).items()):
            if name in RESERVED_NAMES:
                continue
            if kind == "dir" and depth < MAX_DEPTH:
                found += self._files_under(f"{local}/{name}", f"{rel}/{name}", depth + 1)
            else:
                found.append(f"{rel}/{name}")
        return found

    def _thresholds(self) -> Thresholds:
        """EF-EXP-029: `config set export.missing_*` is read on every pass, no restart."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(platform.app_setting.c.key, platform.app_setting.c.value).where(
                    platform.app_setting.c.key.like("export.missing\\_%", escape="\\")
                )
            )
            return thresholds_from({key: value for key, value in rows})

    def _announce_block(self, destination_id: str, reason: str) -> None:
        if self._blocked.get(destination_id) != reason:  # once while the state does not change
            self._blocked[destination_id] = reason
            self._announce(destination_id, "mount.sentinel_missing", reason=reason)
        mark_mount_blocked(self._engine, destination_id, reason)  # `status` shows it

    def _refresh(self, refresh: list[tuple[str, int, int, int, int]], settled: list[str]) -> None:
        if not refresh and not settled:
            return
        record = export.export_record
        with self._engine.begin() as connection:
            for item_id, size, mtime_ns, dev, ino in refresh:
                connection.execute(
                    sa.update(record)
                    .where(record.c.export_item_id == item_id)
                    .values(size=size, mtime_ns=mtime_ns)
                )
                refresh_destination_identity(
                    connection, item_id, dev=dev, ino=ino, size=size, mtime_ns=mtime_ns,
                    verified_at=self._clock.now(),
                )  # fmt: skip
            for item_id in settled:
                connection.execute(
                    sa.update(record)
                    .where(
                        record.c.export_item_id == item_id,
                        record.c.error_code == REEXPORT_ATTEMPTED,
                    )
                    .values(error_code=None)
                )

    def _announce(self, destination_id: str, event_type: str, **payload: str | int) -> None:
        with self._engine.begin() as connection:
            self._bus.publish(connection, event_type, {"destination_id": destination_id, **payload})

    # -- resuming --------------------------------------------------------------------------------

    def _probe_if_denied(self, export_series_id: str) -> None:
        """A series parked for `permission_denied` is freed only by a probe that passes (the
        same one as `target recheck`); otherwise it stays, with the cause and the fix."""
        series = export.export_series
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(series.c.shelf_destination_id, series.c.paused_reason).where(
                    series.c.id == export_series_id
                )
            ).one_or_none()
        if row is None or row.paused_reason != DENIED:
            return
        probe = PermissionPreflight(self._engine, self._fsops, self._bus, clock=self._clock)
        result = probe.check_series(row.shelf_destination_id, export_series_id)
        if result is not None and not result.ok:
            raise DestinationNotWritableError(result)

    def resume_series(self, export_series_id: str) -> bool:
        """`shelfhold series resume` (EF-INT-023): the admin has done the switch-over. What is
        on disk now is the new baseline of the files the manifest knows; unknown files stay
        unknown, so the next pass pauses again if the third party is still there."""
        series, record, item, link = (
            export.export_series, export.export_record, export.export_item,
            export.edition_export_series,
        )  # fmt: skip
        self._probe_if_denied(export_series_id)
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(series.c.shelf_destination_id).where(
                    series.c.id == export_series_id, series.c.paused_reason.isnot(None)
                )
            ).one_or_none()
            if row is None:
                return False
            ctx = load_context(connection, row.shelf_destination_id)
            records = connection.execute(
                sa.select(record.c.export_item_id, record.c.rel_path)
                .select_from(
                    record.join(item, item.c.id == record.c.export_item_id).join(
                        link, link.c.edition_id == item.c.edition_id
                    )
                )
                .where(
                    record.c.shelf_destination_id == ctx.destination_id,
                    record.c.rel_path.isnot(None),
                    record.c.state.in_(LIVE_STATES),
                    link.c.export_series_id == export_series_id,
                )
            ).all()
            for found in records:
                observed = self._fsops.observe(f"{ctx.root_local}/{found.rel_path}")
                if observed is None or observed[0] != "file":
                    continue
                connection.execute(
                    sa.update(record)
                    .where(
                        record.c.shelf_destination_id == ctx.destination_id,
                        record.c.export_item_id == found.export_item_id,
                    )
                    .values(
                        size=observed[1].size, mtime_ns=observed[1].mtime_ns,
                        file_hash=self._fsops.sha256_of(f"{ctx.root_local}/{found.rel_path}"),
                    )
                )  # fmt: skip
                refresh_destination_identity(
                    connection, found.export_item_id,
                    dev=self._fsops.device_of(f"{ctx.root_local}/{found.rel_path}"),
                    ino=observed[1].ino, size=observed[1].size, mtime_ns=observed[1].mtime_ns,
                    verified_at=self._clock.now(),
                )  # fmt: skip
            connection.execute(
                sa.update(series).where(series.c.id == export_series_id).values(paused_reason=None)
            )
        return True

    # -- reading ---------------------------------------------------------------------------------

    @staticmethod
    def _managed(connection: Connection, destination_id: str) -> list[ManagedFile]:
        record, coverage = export.export_record, export.unit_coverage
        rows = connection.execute(
            sa.select(record, coverage.c.unit_id)
            .select_from(
                record.join(
                    coverage,
                    sa.and_(
                        coverage.c.carrier_type == "export_item",
                        coverage.c.carrier_id == record.c.export_item_id,
                    ),
                )
            )
            .where(
                record.c.shelf_destination_id == destination_id, record.c.rel_path.isnot(None),
                record.c.state != "recycled",
            )
            .order_by(record.c.rel_path)
        )  # fmt: skip
        found = list(rows)
        identities = destination_identities(connection, [r.export_item_id for r in found])
        return [
            ManagedFile(
                r.export_item_id, r.unit_id, r.rel_path, r.state, r.size, r.mtime_ns, r.file_hash,
                r.written_by_shelfhold_at, r.error_code == REEXPORT_ATTEMPTED,
                *identities.get(r.export_item_id, (None, None)),
            )
            for r in found
        ]  # fmt: skip

    @staticmethod
    def _series_of(connection: Connection, destination_id: str, item_ids: set[str]) -> list[str]:
        item, link, series = export.export_item, export.edition_export_series, export.export_series
        return list(
            connection.execute(
                sa.select(link.c.export_series_id)
                .select_from(
                    item.join(link, link.c.edition_id == item.c.edition_id).join(
                        series, series.c.id == link.c.export_series_id
                    )
                )
                .where(item.c.id.in_(item_ids), series.c.shelf_destination_id == destination_id)
                .distinct()
            ).scalars()
        )

    def _sources_available(self, gone: list[ManagedFile]) -> set[str]:
        if not gone:
            return set()
        with self._engine.connect() as connection:
            found = export_sources(connection, [f.unit_id for f in gone])
        return {unit for unit, source in found.items() if source.local_path is not None}
