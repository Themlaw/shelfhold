"""Upstream settings taken at adoption, given back at detachment (EF-EXP-056, EF-SRC-033).

`upstream_snapshot` is the durable record of what a connected tool had decided about a series
before Shelfhold followed it (for Suwayomi: its categories and its update strategy). Nothing
else writes it, nothing purges it but a detachment that gave the settings back: not the retention
of the intent journal (which keeps references only), not a restore, not a migration (I31).

`detach --restore-upstream` hands the series back to the tool's own schedule, stops Shelfhold's
follow of them and lists the folders and files Shelfhold named, for the old scripts. It never
deletes or moves a file, and it does not touch Kavita (its settings from before the adoption stay
as they are, EF-EXP-028).
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.upstream_snapshot import (
    CAPABILITY,
    SCHEMA_VERSION,
    SEALED_KEY,
    SnapshotRefusedError,
    open_sealed,
    prepare_payload,
    seal_fields,
    validate_payload,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.refs import SeriesRef

__all__ = [
    "CAPABILITY",
    "SCHEMA_VERSION",
    "DetachReport",
    "DetachScope",
    "Detacher",
    "SeriesDetached",
    "SnapshotRefusedError",
    "TakeReport",
    "UpstreamSnapshots",
    "series_key",
    "validate_payload",
]

log = structlog.get_logger(__name__)
EXCLUSION_MARK: Final = "Shelfhold"  # the name of the category that holds a series out of updates


def series_key(ref: SeriesRef) -> str:
    """`upstream_snapshot.series_ref`: the upstream identity, never the tool's integer id."""
    return f"{ref.source_key}|{ref.remote_id}"


class UpstreamSnapshots:
    def __init__(self, engine: Engine, *, clock: Clock, vault: SecretVault | None = None) -> None:
        self._engine = engine
        self._clock = clock
        self._vault = vault  # seals the fields a service declares sensitive (ENF-SEC-005)

    async def take(
        self, service: Any, f: Fetcher, *, connected_service_id: str, ref: SeriesRef
    ) -> bool:
        """Take the snapshot of a series *before* Shelfhold changes anything upstream. The first
        one wins: a later one would record Shelfhold's own settings. False when the service has
        no `upstream_restore` capability or the series already has its snapshot."""
        if ConnectedCapability.UPSTREAM_RESTORE not in service.connected:
            return False
        key = series_key(ref)
        if self._stored(connected_service_id, key) is not None:
            return False
        sensitive = frozenset(getattr(service, "snapshot_sensitive_fields", ()))
        clear, hidden = prepare_payload(
            await service.snapshot_upstream(f, ref),
            sensitive,
            can_seal=self._vault is not None,
            schema=getattr(service, "snapshot_schema", None),
        )
        row_id = new_ulid()
        if hidden and self._vault is not None:
            clear = {**clear, SEALED_KEY: seal_fields(self._vault, row_id, hidden)}
        with self._engine.begin() as connection:
            if self._row(connection, connected_service_id, key) is not None:
                return False
            connection.execute(
                sa.insert(acquisition.upstream_snapshot).values(
                    id=row_id, connected_service_id=connected_service_id, series_ref=key,
                    capability=CAPABILITY, payload=clear, schema_version=SCHEMA_VERSION,
                    taken_at=self._clock.now(),
                )
            )  # fmt: skip
        return True

    async def take_missing(
        self, service: Any, f: Fetcher, *, connected_service_id: str
    ) -> "TakeReport":
        """Every series Shelfhold follows through an adoption pairing that has no snapshot yet.
        The composition root calls it right after a pairing is confirmed. A series that fails
        does not stop the others; the failures are reported and retried by the next call."""
        report = TakeReport()
        for _, ref in self.followed(connected_service_id, None):
            try:
                if await self.take(service, f, connected_service_id=connected_service_id, ref=ref):
                    report.taken += 1
            except Exception as error:  # one series must not hide the rest
                report.failed.append((ref, type(error).__name__))
        return report

    async def record_exclusions(
        self, service: Any, f: Fetcher, *, connected_service_id: str
    ) -> dict[str, list[int]]:
        """`record_exclusion` for every series Shelfhold follows through an adoption pairing: the
        moment it takes hold of them (right after the snapshot). A series that fails does not
        stop the others; the next call tries it again."""
        recorded: dict[str, list[int]] = {}
        for _, ref in self.followed(connected_service_id, None):
            try:
                added = await self.record_exclusion(
                    service, f, connected_service_id=connected_service_id, ref=ref
                )
            except Exception:  # one series must not hide the rest
                log.warning("upstream.record_exclusion_failed", series=series_key(ref))
                continue
            if added:
                recorded[series_key(ref)] = added
        return recorded

    async def record_exclusion(
        self, service: Any, f: Fetcher, *, connected_service_id: str, ref: SeriesRef
    ) -> list[int]:
        """Once the series is held out of the scheduled updates (after the pairing), remember
        which exclusion categories are new since the snapshot and bear Shelfhold's mark (the
        category named `EXCLUSION_MARK`): the only ones a detachment removes. An exclusion under
        another name is the administrator's own. Returns their ids."""
        key = series_key(ref)
        stored = self._stored(connected_service_id, key)
        if stored is None:
            return []
        snap = {k: v for k, v in stored.items() if k != SEALED_KEY}  # the sealed part stays as is
        known = {int(c["id"]) for c in snap.get("categories", [])}
        now = await service.snapshot_upstream(f, ref)
        added = sorted(
            int(c["id"])
            for c in now.get("categories", [])
            if c.get("include_in_update") == "EXCLUDE"
            and c.get("name") == EXCLUSION_MARK
            and int(c["id"]) not in known
        )
        payload = validate_payload(
            {**snap, "shelfhold_excluded": added}, schema=getattr(service, "snapshot_schema", None)
        )
        if SEALED_KEY in stored:
            payload[SEALED_KEY] = stored[SEALED_KEY]
        table = acquisition.upstream_snapshot
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(
                    table.c.connected_service_id == connected_service_id,
                    table.c.series_ref == key,
                    table.c.capability == CAPABILITY,
                )
                .values(payload=payload)
            )
        return added

    def get(self, connected_service_id: str, key: str) -> dict[str, Any] | None:
        """The snapshot as it was taken, the sealed fields opened."""
        with self._engine.connect() as connection:
            row = self._row(connection, connected_service_id, key)
        if row is None:
            return None
        payload = dict(row.payload)
        blob = payload.pop(SEALED_KEY, None)
        if blob is not None:
            if self._vault is None:
                raise SnapshotRefusedError("a sealed snapshot needs the vault to be read")
            payload.update(open_sealed(self._vault, row.id, blob))
        return payload

    def _stored(self, connected_service_id: str, key: str) -> dict[str, Any] | None:
        with self._engine.connect() as connection:
            row = self._row(connection, connected_service_id, key)
        return dict(row.payload) if row is not None else None

    def count(self, connected_service_id: str | None = None) -> int:
        table = acquisition.upstream_snapshot
        statement = sa.select(sa.func.count()).select_from(table)
        if connected_service_id is not None:
            statement = statement.where(table.c.connected_service_id == connected_service_id)
        with self._engine.connect() as connection:
            return int(connection.execute(statement).scalar_one())

    def forget(self, connected_service_id: str, key: str) -> None:
        """Only after the settings were given back: the one place a snapshot goes."""
        table = acquisition.upstream_snapshot
        with self._engine.begin() as connection:
            connection.execute(
                sa.delete(table).where(
                    table.c.connected_service_id == connected_service_id,
                    table.c.series_ref == key,
                    table.c.capability == CAPABILITY,
                )
            )

    @staticmethod
    def _row(connection: Connection, service_id: str, key: str) -> sa.Row[Any] | None:
        table = acquisition.upstream_snapshot
        return connection.execute(
            sa.select(table)
            .where(
                table.c.connected_service_id == service_id,
                table.c.series_ref == key,
                table.c.capability == CAPABILITY,
            )
            .order_by(table.c.taken_at, table.c.id)
        ).first()

    def followed(
        self, connected_service_id: str, scope: "DetachScope | None"
    ) -> list[tuple[str, SeriesRef]]:
        """`(binding id, upstream series)` of the active bindings paired from an adoption."""
        binding, item, edition = (
            acquisition.source_binding,
            acquisition.source_item,
            catalog.edition,
        )
        statement = (
            sa.select(binding.c.id, item.c.source_key, item.c.remote_id)
            .select_from(
                binding.join(item, item.c.id == binding.c.source_item_id).join(
                    edition, edition.c.id == binding.c.edition_id
                )
            )
            .where(
                item.c.plugin_id == connected_service_id,
                binding.c.active,
                binding.c.linked_from_adoption,
            )
            .order_by(binding.c.id)
        )
        if scope is not None and scope.edition_id is not None:
            statement = statement.where(binding.c.edition_id == scope.edition_id)
        if scope is not None and scope.shelf_id is not None:
            statement = statement.where(edition.c.shelf_id == scope.shelf_id)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()
        return [
            (r.id, SeriesRef(source_key=r.source_key, remote_id=r.remote_id, title=""))
            for r in rows
        ]


@dataclass
class TakeReport:
    taken: int = 0
    failed: list[tuple[SeriesRef, str]] = field(default_factory=list)


@dataclass(frozen=True)
class DetachScope:
    """`--shelf` or `--series` (an Edition); none of them: everything Shelfhold follows."""

    shelf_id: str | None = None
    edition_id: str | None = None


@dataclass(frozen=True)
class SeriesDetached:
    binding_id: str
    ref: SeriesRef
    restored_from: str  # `snapshot` | `default` (no snapshot: only the exclusion goes)
    error: str | None = None
    note: str | None = None  # `snapshot_invalid`: the snapshot was not trusted

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class DetachReport:
    series: list[SeriesDetached] = field(default_factory=list)
    managed: dict[str, list[str]] = field(default_factory=dict)  # folder -> files Shelfhold named
    deleted_files: int = 0  # always 0: nothing is deleted or moved

    @property
    def complete(self) -> bool:
        return all(s.ok for s in self.series)

    @property
    def failed(self) -> list[SeriesDetached]:
        return [s for s in self.series if not s.ok]


class Detacher:
    """`shelfhold detach --restore-upstream`: idempotent and resumable. A series whose settings
    could not be given back keeps its follow and its snapshot; the next run tries it again."""

    def __init__(
        self,
        engine: Engine,
        snapshots: UpstreamSnapshots,
        *,
        connected_service_id: str,
    ) -> None:
        self._engine = engine
        self._snapshots = snapshots
        self._service_id = connected_service_id

    async def restore_upstream(
        self, service: Any, f: Fetcher, scope: DetachScope | None = None
    ) -> DetachReport:
        report = DetachReport()
        followed = self._snapshots.followed(self._service_id, scope)
        for binding_id, ref in followed:
            key = series_key(ref)
            snap = self._snapshots.get(self._service_id, key)
            valid = getattr(service, "snapshot_valid", None)
            note = None
            if snap is not None and valid is not None and not valid(snap):
                snap, note = None, "snapshot_invalid"  # handled as if there were none, and said
                log.warning("upstream.snapshot_invalid", binding_id=binding_id)
            try:
                if ConnectedCapability.UPSTREAM_RESTORE in service.connected:
                    await service.restore_upstream(f, ref, snap or {})
            except Exception as error:  # a series that cannot be restored must not stop the rest
                report.series.append(
                    SeriesDetached(binding_id, ref, "snapshot" if snap else "default",
                                   error=type(error).__name__, note=note)
                )  # fmt: skip
                continue
            kept = getattr(service, "kept_exclusions", None)
            if kept is not None:  # an exclusion that is not Shelfhold's stays, and is said
                try:
                    left = await kept(f, ref)
                except Exception:  # reading it is a courtesy; the restoration is done
                    left = []
                if left:
                    note = "exclusion_kept|" + ", ".join(left)
            self._stop_following(binding_id)
            self._snapshots.forget(self._service_id, key)
            report.series.append(
                SeriesDetached(
                    binding_id, ref, "snapshot" if snap is not None else "default", note=note
                )
            )
        report.managed = self.managed_files([b for b, _ in followed])
        return report

    def _stop_following(self, binding_id: str) -> None:
        """The binding stays (its history and the watermark are kept) but no longer follows."""
        binding = acquisition.source_binding
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(binding)
                .where(binding.c.id == binding_id)
                .values(active=False, disabled_reason="manual")
            )

    def managed_files(self, binding_ids: Iterable[str]) -> dict[str, list[str]]:
        """The folders and files Shelfhold wrote for these series, by the Shelfhold template
        (what the old scripts must now leave alone). Adopted files are not Shelfhold's."""
        ids = list(binding_ids)
        if not ids:
            return {}
        binding, record, item = (
            acquisition.source_binding,
            export.export_record,
            export.export_item,
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(record.c.rel_path)
                .select_from(
                    record.join(item, item.c.id == record.c.export_item_id).join(
                        binding, binding.c.edition_id == item.c.edition_id
                    )
                )
                .where(
                    binding.c.id.in_(ids),
                    record.c.rel_path.is_not(None),
                    record.c.state != "adopted",
                )
                .distinct()
            ).scalars()
            paths = sorted(str(p) for p in rows)
        folders: dict[str, list[str]] = defaultdict(list)
        for path in paths:
            folder, _, name = path.rpartition("/")
            folders[folder].append(name)
        return dict(folders)
