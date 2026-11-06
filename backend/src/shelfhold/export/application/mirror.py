"""Writing the provenance mirror of a root (EF-EXP-063, I33; format in `domain.provenance_mirror`).

The mirror is a projection of the database. After a file is placed and its manifest line
committed, the series file that holds its line is rewritten under an intent (`write`, EF-EXP-041):
the intent is committed, then the file is replaced atomically through `Fsops` (the sole writer,
lease included), then the intent is closed. A crash in between leaves an intent whose target is
either absent or not the new content: boot closes it as abandoned and the next run, or
`rebuild`, writes the projection again. Nothing here ever deletes a line the database cannot
explain (`unresolved`).

Never written: for a destination whose egress is `externe`, for a transport other than `tree`,
for a root that is `adopt_ro` (the lineage of an adopted file is written on the writable root at
the repatriation, GAP-9-16), and never into a file whose format is a newer major.
"""

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.path_map import clean_absolute, within
from shelfhold.export.domain.provenance_mirror import (
    MIRROR_DIR,
    MIRROR_SCHEMA_VERSION,
    MirrorDocument,
    MirrorFormatError,
    merge_with_database,
    mirror_name,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops, PathEscapeError
from shelfhold.infrastructure.schema import acquisition, catalog, export, platform
from shelfhold.integrations.apprise import operating_bus as default_bus
from shelfhold.library.public import export_sources

__all__ = ["MirrorWriter", "ProvenanceMirror"]

log = structlog.get_logger(__name__)

LINE_STATES = ("written", "scanned", "resolved", "adopted")


class MirrorWriter(Protocol):
    """`Fsops`, or a lease-checked write session over it."""

    def write_mirror(self, root_path: str, name: str, data: bytes) -> object: ...


class ProvenanceMirror:
    def __init__(
        self, engine: Engine, fsops: Fsops, *, clock: Clock, operating_bus: EventBus | None = None
    ) -> None:
        self._engine = engine
        # the bus of the application, with its notification channels: a bare `EventBus` would
        # write the event and queue no notification (ENF-OBS-005)
        self._bus = operating_bus or default_bus(clock)
        self._fsops = fsops
        self._clock = clock
        self.skipped: list[str] = []  # files left alone: unreadable, or of a newer major

    # -- the public moves ------------------------------------------------------------------------

    def sync_destination(
        self, destination_id: str, *, folder: str | None = None, writer: MirrorWriter | None = None
    ) -> list[str]:
        """Bring the mirror files of the destination in line with the database; `folder` limits
        the work to the series folder just written. Returns the files actually rewritten."""
        root, pending = self._pending(destination_id, folder)
        written: list[str] = []
        for name, data in pending:
            try:
                written.append(self._put(root, name, data, writer or self._fsops))
            except PathEscapeError:  # a link where the file or its folder should be
                self._skip(name, "symlink")
        return written

    def would_write(self, destination_id: str) -> bool:
        """Is any mirror file of the destination missing or out of date? Writes nothing."""
        return bool(self.drifted(destination_id))

    def drifted(self, destination_id: str) -> list[str]:
        """The mirror files of the destination missing or out of date (`meta check`). Writes
        nothing."""
        return [name for name, _data in self._pending(destination_id, None)[1]]

    def rebuild(self, destination_id: str) -> list[str]:
        """`meta rebuild` for one destination: the whole projection, unresolved lines kept."""
        return self.sync_destination(destination_id)

    def _pending(
        self, destination_id: str, folder: str | None
    ) -> tuple[str, list[tuple[str, bytes]]]:
        with self._engine.connect() as connection:
            ctx = load_context(connection, destination_id)
            root = self._writable_root(connection, ctx)
            if root is None:
                return "", []
            pending: list[tuple[str, bytes]] = []
            for series_id, (header, entries) in self._project(connection, ctx, folder).items():
                name = mirror_name(destination_id, series_id)
                try:
                    raw = self._fsops.read_mirror(root, name)
                    data = self._merged(name, header, entries, raw)
                except PathEscapeError:
                    self._skip(name, "symlink")
                    continue
                except Exception:  # a forged or damaged mirror never stops an export
                    self._skip(name, "unreadable")
                    continue
                if data is not None and data != raw:
                    pending.append((name, data))
        return root, pending

    # -- what to write, where --------------------------------------------------------------------

    @staticmethod
    def _writable_root(connection: Connection, ctx: DestinationContext) -> str | None:
        """The physical root the destination's tree lives on, if a mirror may go there."""
        if (
            ctx.transport != "tree"
            or ctx.egress == "externe"
            or ctx.root_local is None
            or ctx.remote_library_id is None
        ):
            return None
        best: tuple[str, str] | None = None
        table = platform.physical_root
        for row in connection.execute(sa.select(table.c.path_entry, table.c.mode)):
            path = clean_absolute(row.path_entry)
            if within(clean_absolute(ctx.root_local), path) is not None and (
                best is None or len(path) > len(best[0])
            ):
                best = (path, row.mode)
        return best[0] if best is not None and best[1] == "rw" else None

    def _skip(self, name: str, reason: str) -> None:
        """Left alone and reported (`reconcile.drift`); the export goes on."""
        self.skipped.append(name)
        log.warning("mirror_skipped", file=name, reason=reason)
        try:
            with self._engine.begin() as connection:
                self._bus.publish(
                    connection,
                    "reconcile.drift",
                    {"mirror_files": 1},  # the reason is in the log
                )
        except Exception:  # reporting must not fail the run either
            log.warning("mirror_event_failed", reason=reason)

    def _merged(
        self,
        name: str,
        header: dict[str, Any],
        entries: list[dict[str, Any]],
        raw: bytes | None,
    ) -> bytes | None:
        if raw is None:
            return MirrorDocument(MIRROR_SCHEMA_VERSION, header, entries).dumps()
        try:
            on_disk = MirrorDocument.parse(raw)
        except MirrorFormatError:
            self._skip(name, "format")  # damaged beyond reading: never overwritten
            return None
        if on_disk.read_only:
            self._skip(name, "newer_major")  # read only (ADR-0039)
            return None
        return merge_with_database(on_disk, entries, header=header).dumps()

    def _put(self, root: str, name: str, data: bytes, writer: MirrorWriter) -> str:
        """The intent (`write`), then the file, then the intent closed (EF-EXP-041)."""
        target = f"{root}/{MIRROR_DIR}/{name}"
        journal = IntentJournal(self._engine, self._fsops, clock=self._clock)
        intent = journal.record(
            op="write", dst=target,
            expected_sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        )  # fmt: skip
        journal.mark_applying(intent)
        try:
            writer.write_mirror(root, name, data)
        except Exception:
            journal.cancel(intent)  # nothing was placed (the write is atomic)
            raise
        journal.mark_done(intent)
        return target

    # -- the projection --------------------------------------------------------------------------

    def _project(
        self, connection: Connection, ctx: DestinationContext, folder: str | None
    ) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
        record, item, link = export.export_record, export.export_item, export.edition_export_series
        query = (
            sa.select(
                record.c.export_item_id, record.c.rel_path, record.c.file_hash,
                record.c.content_hash, record.c.template_ref,
                record.c.written_by_shelfhold_at, record.c.exported_at,
                item.c.kind, item.c.frozen_attrs, item.c.edition_id,
                link.c.export_series_id,
                catalog.edition.c.age_rating, catalog.edition.c.age_x,
            )
            .select_from(
                record.join(item, item.c.id == record.c.export_item_id)
                .join(
                    link,
                    sa.and_(
                        link.c.edition_id == item.c.edition_id,
                        link.c.remote_library_id == ctx.remote_library_id,
                    ),
                )
                .join(catalog.edition, catalog.edition.c.id == item.c.edition_id)
            )
            .where(
                record.c.shelf_destination_id == ctx.destination_id,
                record.c.locator_type == "path",
                record.c.rel_path.is_not(None),
                record.c.file_hash.is_not(None),
                record.c.state.in_(LINE_STATES),
            )
            .order_by(link.c.export_series_id, record.c.rel_path)
        )  # fmt: skip
        if folder is not None:
            escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(record.c.rel_path.like(escaped + "/%", escape="\\"))
        rows = connection.execute(query).all()
        coverage = self._coverage(connection, [r.export_item_id for r in rows])
        grouped: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        sources = self._sources(connection, coverage)
        for row in rows:
            covered = coverage[row.export_item_id]
            source = sources.get(covered[0]["unit"]) if len(covered) == 1 else None
            line: dict[str, Any] = {
                "kind": "entry", "state": "exported", "rel_path": row.rel_path,
                "file_hash": row.file_hash, "content_hash": row.content_hash,
                "content_hash_by": source.content_hash_by if source else None,
                "export_item": {
                    "id": row.export_item_id, "kind": row.kind,
                    "frozen_attrs": dict(row.frozen_attrs or {}),
                },
                "coverage": covered, "template_ref": row.template_ref,
                "edition": row.edition_id, "series": row.export_series_id,
                "written_at": _stamp(row.written_by_shelfhold_at or row.exported_at),
            }  # fmt: skip
            if source is not None and source.derived_hash:
                line["derived_hash"], line["derived_by"] = source.derived_hash, source.derived_by
            if row.age_rating is not None:  # read back as a suggestion only (GAP-10-13)
                line["age_classified"] = {"min_age": row.age_rating, "x": bool(row.age_x)}
            if row.export_series_id not in grouped:
                grouped[row.export_series_id] = (self._header(connection, ctx, row), [])
            grouped[row.export_series_id][1].append(line)
        return grouped

    @staticmethod
    def _header(connection: Connection, ctx: DestinationContext, row: Any) -> dict[str, Any]:
        header: dict[str, Any] = {
            "kind": "header", "schema_version": MIRROR_SCHEMA_VERSION,
            "shelf_destination": ctx.destination_id, "export_series": row.export_series_id,
            "edition": row.edition_id,
        }  # fmt: skip
        binding = connection.execute(
            sa.select(acquisition.source_item.c.plugin_id, acquisition.source_item.c.source_key,
                      acquisition.source_item.c.remote_id)
            .select_from(
                acquisition.source_binding.join(
                    acquisition.source_item,
                    acquisition.source_item.c.id == acquisition.source_binding.c.source_item_id,
                )
            )
            .where(acquisition.source_binding.c.edition_id == row.edition_id)
            .order_by(acquisition.source_binding.c.active.desc(),
                      acquisition.source_binding.c.priority, acquisition.source_binding.c.id)
            .limit(1)
        ).first()  # fmt: skip
        if binding is not None:  # a reference to the remote series: never a URL (EF-EXP-063)
            header["binding"] = {
                "plugin_id": binding.plugin_id, "source_key": binding.source_key,
                "sub_source": None, "series_ref": binding.remote_id,
            }  # fmt: skip
        follow = connection.execute(
            sa.select(acquisition.series_follow.c.follow_from_segment,
                      acquisition.series_follow.c.follow_from_number)
            .where(acquisition.series_follow.c.edition_id == row.edition_id)
        ).first()  # fmt: skip
        if follow is not None and follow.follow_from_number is not None:
            header["follow_from"] = {
                "segment": follow.follow_from_segment, "number": follow.follow_from_number,
            }  # fmt: skip
        return header

    @staticmethod
    def _coverage(
        connection: Connection, item_ids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        table = export.unit_coverage
        found: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            sa.select(table)
            .where(table.c.carrier_type == "export_item", table.c.carrier_id.in_(item_ids))
            .order_by(table.c.carrier_id, table.c.position)
        ):
            found[row.carrier_id].append(
                {"unit": row.unit_id, "start": row.start_locator, "end": row.end_locator}
            )
        return found

    @staticmethod
    def _sources(
        connection: Connection, coverage: dict[str, list[dict[str, Any]]]
    ) -> dict[str, Any]:
        """Fill each coverage entry with its Variant, source reference and team, and return the
        archives received per Unit (for the hash functions)."""
        unit_ids = sorted({c["unit"] for entries in coverage.values() for c in entries})
        received = export_sources(connection, unit_ids)
        variant, binding, source_item = (
            catalog.unit_variant, acquisition.source_binding, acquisition.source_item,
        )  # fmt: skip
        chosen: dict[str, str] = {}
        rows = connection.execute(
            sa.select(variant.c.id, variant.c.unit_id)
            .where(variant.c.unit_id.in_(unit_ids))
            .order_by(variant.c.preferred.desc(), variant.c.first_seen_at, variant.c.id)
        )
        fallback: dict[str, str] = {}
        for row in rows:
            fallback.setdefault(row.unit_id, row.id)
        for unit in unit_ids:
            source = received.get(unit)
            chosen[unit] = (source.variant_id if source and source.variant_id else None) or (
                fallback.get(unit, "")
            )
        details = {
            row.id: row
            for row in connection.execute(
                sa.select(
                    variant.c.id, variant.c.remote_unit_id, variant.c.team_id,
                    source_item.c.source_key, catalog.team.c.label,
                )
                .select_from(
                    variant.join(binding, binding.c.id == variant.c.source_binding_id)
                    .join(source_item, source_item.c.id == binding.c.source_item_id)
                    .outerjoin(catalog.team, catalog.team.c.id == variant.c.team_id)
                )
                .where(variant.c.id.in_(set(chosen.values())))
            )
        }  # fmt: skip
        suppressed = set(
            connection.execute(
                sa.select(catalog.unit.c.id).where(
                    catalog.unit.c.id.in_(unit_ids),
                    catalog.unit.c.follow_suppressed_at.is_not(None),
                )
            ).scalars()
        )
        for entries in coverage.values():
            for covered in entries:
                detail = details.get(chosen.get(covered["unit"], ""))
                covered.update(
                    variant=chosen.get(covered["unit"]) or None,
                    source_key=detail.source_key if detail else None,
                    sub_source=None,
                    remote_unit_id=detail.remote_unit_id if detail else None,
                    team=(
                        {"id": detail.team_id, "label": detail.label}
                        if detail and detail.team_id
                        else None
                    ),
                )
                if covered["unit"] in suppressed:
                    covered["follow_suppressed"] = True
        return dict(received)


def _stamp(moment: Any) -> str | None:
    return moment.isoformat() if moment is not None else None
