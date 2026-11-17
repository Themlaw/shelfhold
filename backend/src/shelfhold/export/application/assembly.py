"""Chapters served one by one become one volume (EF-EXP-019, EF-BIB-044, I20, M4 L10, intouchable).

`replace(old_items[], new_item)` under **one** intent (`assemble`): the chapter files of a
destination are replaced by the volume that carries them. In order, each step one the boot can
tell from the disk:

1. the volume is built whole in the staging folder (the pages of the chapter files, in reading
   order, in the formats the destination takes, EF-EXP-046) and checked against its own hash;
2. the intent is recorded and marked `applying`, naming the volume, every chapter file with its
   hash and the bin entry it will go to;
3. the volume is written at its path (`write_atomic`: never visible truncated, never over a file);
4. each chapter file goes to the bin of its root (`Fsops.recycle`, never unlinked);
5. the manifest, the coverage, the aliases, the registry of copies, the event and `done`, in one
   transaction.

A SIGKILL between two steps leaves a state `IntentJournal._resolve_assemble` settles: nothing
written means nothing happened (the chapters are untouched, the intent is closed); the volume
written means the chapters still served are binned and the bookkeeping is done. The destination
never keeps the volume next to the chapters it replaced, and no chapter is ever lost.

The volume is a derivative: it covers its chapters in `unit_coverage` (`origin = manual`, bounds in
pages) and is never a copy of them. Its hash says what it was made of (`derived_content_hash`), no
`replica` is told for it, and `independent_copies` does not change. The export items of the
chapters stay, with the positions written on them: the progress of each member is kept, and a
position on the volume is read through the coverage, which marks nothing read that was not
reached (`reading_progress`, L14a). The hash of the file is another one: KOReader starts the
document again, `status` says so (D21), and the old hashes still resolve the chapters
(`document_alias`).

A volume carries one Edition (one rating, one shelf): chapters of two Editions are never assembled,
and the barrier of the export (the destination's state, the shelf's ceiling) is looked at again
before anything is written.
"""

from __future__ import annotations

import enum
import re
import tempfile
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.export.application import render_hooks
from shelfhold.export.application.fan_out import mirror_to_indexers
from shelfhold.export.application.intents import (
    IntentJournal,
    ManifestHook,
    OpenIntent,
    open_intent_paths,
)
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.mirror import ProvenanceMirror
from shelfhold.export.application.rendering import CbzRenderer, comicinfo_data
from shelfhold.export.application.snapshot import (
    DestinationContext,
    barrier_closed_by,
    load_context,
)
from shelfhold.export.domain.naming import FrozenAttrs, export_path
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust import check_cbz_entries
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import escaped
from shelfhold.infrastructure.packaging.cbz import write_cbz
from shelfhold.infrastructure.packaging.comicinfo import render_comicinfo
from shelfhold.infrastructure.schema import catalog, export, reading
from shelfhold.library.public import (
    ChapterSpan,
    RootLocator,
    VolumeCoverageError,
    derived_content_hash,
    forget_destination_copies,
    record_binned_export_file,
    spans_of,
    volume_page_of,
)
from shelfhold.reading.application.progress import progress_of

__all__ = [
    "EVENT",
    "PAYLOAD_KIND",
    "REASON",
    "AssemblyOutcome",
    "AssemblyRequest",
    "AssemblyResult",
    "VolumeAssembly",
    "served_chapters",
]

log = structlog.get_logger(__name__)

EVENT = (
    "unit.upgraded"  # KOReader's position on the document is reset: the one event `status` reads
)
REASON = "volume_assembly"
PAYLOAD_KIND = "assembly"
MIN_CHAPTERS = 2
_STARTED = ("in_progress", "read")
_VOLUME = re.compile(r"[0-9]{1,6}")  # the `Volume` of a ComicInfo is a whole number


class AssemblyOutcome(enum.Enum):
    ASSEMBLED = "assembled"
    TOO_FEW = "too_few"  # a volume is made of two chapters at least
    INVALID_VOLUME = "invalid_volume"
    NOT_SERVED = "not_served"  # an item is not a file this destination serves (a stranger's too)
    NOT_CHAPTERS = "not_chapters"  # an item is a volume, or covers several Units: never again
    MIXED_EDITIONS = "mixed_editions"  # one volume carries one Edition: one rating, one shelf
    TARGET_CHANGED = "target_changed"  # a chapter file is not the one the manifest knows
    ROOT_FROZEN = (
        "root_frozen"  # the root (or the instance) is frozen: nothing, not even the intent
    )
    REFUSED_DESTINATION = "refused_destination"  # paused, read-only, not a folder of this machine
    REFUSED_AGE = "refused_age"  # the shelf below 18+ no longer admits the Edition (EF-BIB-037)
    BUSY = "busy"  # another intent holds a path
    NAME_TAKEN = "name_taken"  # the volume's name is served or reserved: never overwritten
    GAP = "gap"  # a chapter between the first and the last is known but not served
    SOURCE_UNREADABLE = "source_unreadable"  # a chapter file cannot be read as a CBZ, or is too big


@dataclass(frozen=True)
class AssemblyRequest:
    destination_id: str
    export_item_ids: tuple[str, ...]  # the chapter files to replace, any order
    volume: str  # the number the volume is named with
    job_id: str | None = None  # holds the locks of the Work and the series folder when given


@dataclass(frozen=True)
class AssemblyResult:
    outcome: AssemblyOutcome
    export_item_id: str | None = None
    assembly_id: str | None = None
    missing: tuple[str, ...] = ()  # `gap`: the numbers known but not served, in reading order


@dataclass(frozen=True)
class _Old:
    export_item_id: str
    unit_id: str
    rel_path: str
    file_hash: str
    content_hash: str | None
    series: str
    template_ref: str | None
    number_sort: float | None
    source_order: int
    number: str


def _numeric(value: str) -> Decimal:
    try:
        return Decimal(value)
    except ArithmeticError:
        return Decimal(0)


class VolumeAssembly:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        *,
        locate: RootLocator,
        data_dir: Path,
        clock: Clock,
        bus: EventBus | None = None,
        renderer: CbzRenderer | None = None,
        mirror: ProvenanceMirror | None = None,
        request_scan: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine
        self._fsops: Fsops = fsops
        self._locate = locate
        self._clock = clock
        self._staging = data_dir / "tmp"
        self._bus = bus or EventBus(clock)
        self._renderer = renderer or CbzRenderer(fsops, staging_dir=self._staging)
        self._mirror = mirror or ProvenanceMirror(engine, fsops, clock=clock)
        self._request_scan = request_scan
        self._journal = IntentJournal(engine, fsops, clock=clock)
        self._locks = ResourceLocks(engine, clock=clock)

    # -- the door ---------------------------------------------------------------------------

    def assemble(self, request: AssemblyRequest) -> AssemblyResult:
        """Replace the chapter files `request.export_item_ids` of one destination by one volume."""
        if _VOLUME.fullmatch(request.volume) is None:
            return AssemblyResult(AssemblyOutcome.INVALID_VOLUME)
        ids = tuple(dict.fromkeys(request.export_item_ids))
        if len(ids) < MIN_CHAPTERS:
            return AssemblyResult(AssemblyOutcome.TOO_FEW)
        with self._engine.connect() as connection:
            try:
                ctx = load_context(connection, request.destination_id)
            except (LookupError, sa.exc.NoResultFound):
                return AssemblyResult(AssemblyOutcome.NOT_SERVED)
            loaded = self._load(connection, ctx, ids)
            if isinstance(loaded, AssemblyOutcome):
                return AssemblyResult(loaded)
            olds, edition_id, work_id = loaded
            missing = _unserved_between(connection, ctx.destination_id, edition_id, olds)
            if missing:
                log.warning("assembly.gap", missing=len(missing))
                return AssemblyResult(AssemblyOutcome.GAP, missing=missing)
            refused = self._refusal(connection, ctx, olds, edition_id)
            if refused is not None:
                return AssemblyResult(refused)
        folder = olds[0].rel_path.split("/", 1)[0]
        frozen = FrozenAttrs(
            series=olds[0].series, track="main", number_norm=request.volume, kind="volume",
            template_ref=ctx.template.ref,
        )  # fmt: skip
        path = export_path(frozen, ctx.template, ".cbz")
        rel = "/".join((folder, *path.directory[1:], path.filename))
        with self._locks.hold(
            request.job_id or new_ulid(), work=work_id,
            series=f"{ctx.destination_id}/{folder}", timeout=30.0,
        ):  # fmt: skip
            return self._assemble_held(ctx, olds, edition_id, request.volume, frozen, rel)

    def settle_hook(self) -> ManifestHook:
        """For `IntentJournal.resolve_open(on_done=...)` at boot: the bookkeeping of an `assemble`
        intent the replay finished. Other operations are not ours."""

        def settle(connection: Connection, intent: OpenIntent) -> None:
            if intent.op != "assemble" or intent.payload.get("kind") != PAYLOAD_KIND:
                return
            self._complete(connection, intent.id, intent.payload)

        return settle

    # -- what is asked, looked at -----------------------------------------------------------

    @staticmethod
    def _load(
        connection: Connection, ctx: DestinationContext, ids: Sequence[str]
    ) -> tuple[list[_Old], str, str] | AssemblyOutcome:
        record, item, cover = export.export_record, export.export_item, export.unit_coverage
        unit = catalog.unit
        found: list[_Old] = []
        editions: set[str] = set()
        for item_id in ids:
            row = connection.execute(
                sa.select(
                    record.c.rel_path, record.c.file_hash, record.c.content_hash, record.c.state,
                    record.c.locator_type, item.c.kind, item.c.edition_id, item.c.frozen_attrs,
                )
                .select_from(record.join(item, item.c.id == record.c.export_item_id))
                .where(
                    record.c.shelf_destination_id == ctx.destination_id,
                    record.c.export_item_id == item_id,
                )
            ).one_or_none()  # fmt: skip
            if (
                row is None
                or row.locator_type != "path"
                or row.state != "written"
                or not row.file_hash
                or not row.rel_path
            ):
                return AssemblyOutcome.NOT_SERVED
            units = connection.execute(
                sa.select(unit.c.id, unit.c.number_sort, unit.c.source_order, unit.c.number_norm)
                .select_from(cover.join(unit, unit.c.id == cover.c.unit_id))
                .where(cover.c.carrier_type == "export_item", cover.c.carrier_id == item_id)
            ).all()
            if row.kind != "unit" or len(units) != 1:
                return AssemblyOutcome.NOT_CHAPTERS
            editions.add(str(row.edition_id))
            attrs = dict(row.frozen_attrs or {})
            found.append(
                _Old(
                    item_id, str(units[0].id), str(row.rel_path), str(row.file_hash),
                    row.content_hash, str(attrs.get("series", "")),
                    str(attrs["template_ref"]) if attrs.get("template_ref") else None,
                    units[0].number_sort, int(units[0].source_order), str(units[0].number_norm),
                )
            )  # fmt: skip
        if len(editions) != 1:
            return AssemblyOutcome.MIXED_EDITIONS
        found.sort(
            key=lambda o: (
                o.number_sort if o.number_sort is not None else float("inf"),
                o.source_order,
                _numeric(o.number),
                o.unit_id,
            )
        )
        (edition_id,) = editions
        work_id = connection.execute(
            sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == edition_id)
        ).scalar_one()
        return found, edition_id, str(work_id)

    def _refusal(
        self, connection: Connection, ctx: DestinationContext, olds: Sequence[_Old], edition_id: str
    ) -> AssemblyOutcome | None:
        if ctx.transport != "tree" or ctx.root_local is None:
            return AssemblyOutcome.REFUSED_DESTINATION  # no file of this machine to replace
        for old in olds:
            local = ctx.local(old.rel_path)
            root_id, _rel = self._locate(local)
            if (
                self._fsops.frozen
                or self._fsops.freeze_state.frozen_for(root_id)
                or self._fsops.status(local).state is not RootState.OK
            ):  # frozen, or blocked by its sentinel or its lease: nothing, not even the intent
                log.warning("assembly.root_frozen", root_id=root_id)
                return AssemblyOutcome.ROOT_FROZEN
        closed = barrier_closed_by(connection, ctx.destination_id, edition_id)
        if closed is not None:
            log.info("assembly.refused_barrier", why=closed)
            return (
                AssemblyOutcome.REFUSED_DESTINATION if closed == "destination"
                else AssemblyOutcome.REFUSED_AGE
            )  # fmt: skip
        busy = open_intent_paths(connection)
        if any(ctx.local(o.rel_path) in busy for o in olds):
            return AssemblyOutcome.BUSY
        return None

    # -- the assembly -----------------------------------------------------------------------

    def _assemble_held(
        self,
        ctx: DestinationContext,
        olds: Sequence[_Old],
        edition_id: str,
        volume: str,
        frozen: FrozenAttrs,
        rel: str,
    ) -> AssemblyResult:
        target = ctx.local(rel)
        if self._name_taken(ctx, rel, target):
            return AssemblyResult(AssemblyOutcome.NAME_TAKEN)
        for old in olds:
            local = ctx.local(old.rel_path)
            found = self._fsops.hash_and_identity(local)
            if found is None or found[0] != old.file_hash:
                log.warning("assembly.target_changed", path=escaped(local))
                return AssemblyResult(AssemblyOutcome.TARGET_CHANGED)
        try:
            counts = [(o.unit_id, self._page_count(ctx.local(o.rel_path))) for o in olds]
            spans = spans_of(counts)
            with self._engine.connect() as connection:
                data = comicinfo_data(connection, olds[0].unit_id, olds[0].series)
                target_id = ctx.target_id
            data = replace(
                data, number="", title="", count=None, volume=int(volume),
                page_count=spans[-1].end_page,
            )  # fmt: skip
            xml = render_comicinfo(data).xml
            with tempfile.TemporaryFile(dir=self._staging) as raw:
                write_cbz(self._pages(ctx, olds), b'<?xml version="1.0"?><ComicInfo/>', raw)
                raw.flush()
                raw.seek(0)
                rendered = self._renderer.render_stream(
                    raw, "assembled.cbz", xml, target_id=target_id
                )
        except (ValueError, OSError, VolumeCoverageError) as error:
            log.warning("assembly.source_unreadable", error=type(error).__name__)
            return AssemblyResult(AssemblyOutcome.SOURCE_UNREADABLE)
        try:
            return self._write(ctx, olds, edition_id, volume, frozen, rel, spans, rendered)
        finally:
            rendered.file.close()

    def _name_taken(self, ctx: DestinationContext, rel: str, target: str) -> bool:
        record = export.export_record
        with self._engine.connect() as connection:
            reserved = connection.execute(
                sa.select(sa.literal(1)).where(
                    record.c.shelf_destination_id == ctx.destination_id,
                    record.c.rel_path == rel,
                    record.c.state != "recycled",
                )
            ).first()
            busy = target in open_intent_paths(connection)
        return reserved is not None or busy or self._fsops.observe(target) is not None

    def _page_count(self, local: str) -> int:
        with (
            self._fsops.open_read(local) as stream,
            safe_open_archive(stream, name=Path(local).name) as archive,
        ):
            return len(check_cbz_entries(archive).images)

    def _pages(self, ctx: DestinationContext, olds: Sequence[_Old]) -> Iterator[tuple[str, bytes]]:
        """The pages of every chapter file in reading order, named so that the order holds under
        any sort (`c0001_p00001.png`): a name of a chapter never meets one of another."""
        for rank, old in enumerate(olds, start=1):
            local = ctx.local(old.rel_path)
            with (
                self._fsops.open_read(local) as stream,
                safe_open_archive(stream, name=Path(local).name) as archive,
            ):
                for page, name in enumerate(check_cbz_entries(archive).images, start=1):
                    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
                    yield f"c{rank:04d}_p{page:05d}.{suffix}", bytes(archive.read(name))

    def _write(
        self,
        ctx: DestinationContext,
        olds: Sequence[_Old],
        edition_id: str,
        volume: str,
        frozen: FrozenAttrs,
        rel: str,
        spans: Sequence[ChapterSpan],
        rendered: Any,
    ) -> AssemblyResult:
        target = ctx.local(rel)
        new_item_id = new_ulid()
        entries = []
        for old in olds:
            local = ctx.local(old.rel_path)
            root_id, root_rel = self._locate(local)
            entries.append({
                "export_item_id": old.export_item_id, "unit_id": old.unit_id, "local": local,
                "sha256": old.file_hash, "entry": uuid.uuid4().hex, "root_id": root_id,
                "root_rel": root_rel,
            })  # fmt: skip
        derived = derived_content_hash(o.content_hash or o.file_hash for o in olds)
        payload: dict[str, Any] = {
            "kind": PAYLOAD_KIND, "destination_id": ctx.destination_id,
            "export_item_id": new_item_id, "edition_id": edition_id, "volume": volume,
            "rel_path": rel, "frozen": asdict(frozen), "olds": entries,
            "old_paths": [e["local"] for e in entries], "derived_hash": derived,
            "spans": [[s.unit_id, s.start_page, s.end_page] for s in spans],
            "size": rendered.size,
        }  # fmt: skip
        intent = self._journal.record(
            op="assemble", dst=target, expected_sha256=rendered.file_hash, extra=payload
        )
        self._journal.mark_applying(intent)
        try:
            self._fsops.write_atomic(
                target, rendered.file, replace=False,
                verify_digest=lambda: rendered.file_hash.removeprefix("sha256:"),
            )  # fmt: skip
        except BaseException:
            # nothing was placed (the write is atomic and refuses a file already there): the
            # chapters were not touched, the intent is closed
            self._journal.cancel(intent)
            raise
        # From here an error leaves the intent open: the boot (or `intents resolve`) settles it.
        for entry in entries:
            self._fsops.recycle(str(entry["local"]), entry=str(entry["entry"]))
        with write_transaction(self._engine) as connection:
            assembly_id, events = self._complete(
                connection, intent, {**payload, "kind": PAYLOAD_KIND}
            )
            self._journal.mark_done(intent, within=connection)
        self._after(ctx, rel)
        self._bus.deliver(events)
        log.info("assembly.done", chapters=len(olds), volume=volume)
        return AssemblyResult(AssemblyOutcome.ASSEMBLED, new_item_id, assembly_id)

    def _after(self, ctx: DestinationContext, rel: str) -> None:
        """The provenance mirror of the folder, then Kavita is asked to look at it again; the
        files are placed and committed, so a failure here is healed by the next run."""
        folder = rel.split("/", 1)[0]
        try:
            self._mirror.sync_destination(ctx.destination_id, folder=folder)
        except Exception:
            log.warning("mirror_write_failed", destination=ctx.destination_id)
        if self._request_scan is not None:
            self._request_scan(ctx.local(folder))

    # -- the bookkeeping, in the transaction that closes the intent -------------------------

    def _complete(
        self, connection: Connection, intent_id: str, payload: dict[str, Any]
    ) -> tuple[str, list[PublishedEvent]]:
        """The volume is at its path and the chapter files are in the bin: the export item and its
        coverage, the manifest line, the chapter lines, the aliases, the bin entries, the registry
        of copies, `export_assembly` and `unit.upgraded`. Runs once per intent (the journal closes
        it in the same transaction); a second call finds the row and does nothing."""
        table = export.export_assembly
        existing = connection.execute(
            sa.select(table.c.id).where(table.c.intent_id == intent_id)
        ).scalar()
        if existing is not None:
            return str(existing), []
        row = connection.execute(
            sa.text("SELECT dst_path, expected_sha256 FROM fs_intent WHERE id = :i"),
            {"i": intent_id},
        ).one_or_none()
        if row is None:
            raise LookupError(f"assemble intent {intent_id} is gone")
        target, new_hash = str(row.dst_path), str(row.expected_sha256)
        destination_id = str(payload["destination_id"])
        item_id = str(payload["export_item_id"])
        now = self._clock.now()
        observed = self._fsops.observe(target)
        if observed is None:
            raise FileNotFoundError(target)
        identity = observed[1]
        item, record, cover = export.export_item, export.export_record, export.unit_coverage
        frozen = dict(payload["frozen"])
        connection.execute(
            sa.insert(item).values(
                id=item_id, edition_id=str(payload["edition_id"]), kind="volume",
                frozen_attrs=frozen, closed=True, content_id=None,
            )
        )  # fmt: skip
        for rank, (unit_id, first, last) in enumerate(payload["spans"], start=1):
            connection.execute(
                sa.insert(cover).values(
                    carrier_type="export_item", carrier_id=item_id, unit_id=unit_id,
                    position=rank, start_locator={"page": first}, end_locator={"page": last},
                    extent="full", origin="manual",
                )
            )  # fmt: skip
        connection.execute(
            sa.insert(record).values(
                shelf_destination_id=destination_id, export_item_id=item_id, locator_type="path",
                rel_path=str(payload["rel_path"]), template_ref=frozen.get("template_ref"),
                format="cbz", size=identity.size, mtime_ns=identity.mtime_ns,
                content_hash=str(payload["derived_hash"]), file_hash=new_hash,
                written_by_shelfhold_at=now, exported_at=now, state="written",
            )
        )  # fmt: skip
        hashes = render_hooks.sample_hashes(Path(target))
        connection.execute(
            sa.insert(export.document_alias).values(
                export_item_id=item_id, file_hash=new_hash,
                content_hash=str(payload["derived_hash"]), shelf_destination_id=destination_id,
                koreader_partial_md5=hashes.koreader_partial_md5 if hashes else None,
                kavita_md5=hashes.kavita_md5 if hashes else None,
            )
        )  # fmt: skip
        listed: list[dict[str, Any]] = []
        for old in payload["olds"]:
            old_item = str(old["export_item_id"])
            connection.execute(
                sa.update(record)
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == old_item,
                    record.c.state == "written",
                )
                .values(state="recycled", updated_at=now)
            )
            forget_destination_copies(connection, old_item, physical_root_id=str(old["root_id"]))
            trash_id = record_binned_export_file(
                connection, export_item_id=old_item, physical_root_id=str(old["root_id"]),
                path=f"{old['entry']}/{old['root_rel']}", size=None, now=now,
            )  # fmt: skip
            mirror_to_indexers(connection, destination_id, old_item)
            listed.append({
                "export_item_id": old_item, "unit_id": old["unit_id"],
                "file_hash": old["sha256"], "trash_item_id": trash_id,
            })  # fmt: skip
        mirror_to_indexers(connection, destination_id, item_id)
        assembly_id = new_ulid()
        connection.execute(
            sa.insert(table).values(
                id=assembly_id, intent_id=intent_id, shelf_destination_id=destination_id,
                export_item_id=item_id, volume=str(payload["volume"]), old_items=listed,
                derived_hash=str(payload["derived_hash"]), file_hash=new_hash, created_at=now,
            )
        )  # fmt: skip
        carried = _carry_positions(connection, payload, now)
        log.info("assembly.carried", profiles=carried)
        events = [
            self._bus.publish(
                connection, EVENT,
                {
                    "unit_id": str(unit_id), "export_item_id": item_id,
                    "shelf_destination_id": destination_id, "assembly_id": assembly_id,
                    "reason": REASON, "automatic": False, "koreader_reset": True,
                },
                visibility="ops",
            )
            for unit_id, _first, _last in payload["spans"]
        ]  # fmt: skip
        return assembly_id, events


def _unserved_between(
    connection: Connection, destination_id: str, edition_id: str, olds: Sequence[_Old]
) -> tuple[str, ...]:
    """The chapters of the Edition that lie between the first and the last of `olds`, that the
    catalog knows and this destination serves in no file: a volume that skips them would hide them
    from the reader, then the chapter would arrive alone after the volume. A chapter inside
    another volume is served."""
    unit, record, cover = catalog.unit, export.export_record, export.unit_coverage
    numbers = [_numeric(o.number) for o in olds]
    low, high = min(numbers), max(numbers)
    own = {o.unit_id for o in olds}
    served = (
        sa.select(cover.c.unit_id)
        .select_from(
            cover.join(
                record,
                sa.and_(
                    cover.c.carrier_type == "export_item",
                    cover.c.carrier_id == record.c.export_item_id,
                ),
            )
        )
        .where(record.c.shelf_destination_id == destination_id, record.c.state == "written")
    )
    rows = connection.execute(
        sa.select(unit.c.id, unit.c.number_norm, unit.c.number_sort, unit.c.source_order).where(
            unit.c.edition_id == edition_id, unit.c.track == "main", unit.c.id.not_in(served)
        )
    ).all()
    gaps = [r for r in rows if str(r.id) not in own and low < _numeric(str(r.number_norm)) < high]
    gaps.sort(key=lambda r: (_numeric(str(r.number_norm)), int(r.source_order), str(r.id)))
    return tuple(str(r.number_norm) for r in gaps)


def served_chapters(
    connection: Connection, destination_id: str, edition_id: str, first: Decimal, last: Decimal
) -> list[str]:
    """The export items of the chapters `first` to `last` of an Edition that this destination
    serves as a file of their own (never a volume, never a file of several Units), in reading
    order: what `library assemble-volume` hands to `assemble`."""
    record, item, cover, unit = (
        export.export_record, export.export_item, export.unit_coverage, catalog.unit,
    )  # fmt: skip
    rows = connection.execute(
        sa.select(item.c.id, unit.c.number_norm, unit.c.number_sort, unit.c.source_order)
        .select_from(
            record.join(item, item.c.id == record.c.export_item_id)
            .join(
                cover,
                sa.and_(cover.c.carrier_type == "export_item", cover.c.carrier_id == item.c.id),
            )
            .join(unit, unit.c.id == cover.c.unit_id)
        )
        .where(
            record.c.shelf_destination_id == destination_id,
            record.c.state == "written",
            item.c.edition_id == edition_id,
            item.c.kind == "unit",
            unit.c.track == "main",
        )
    ).all()
    chosen = [r for r in rows if first <= _numeric(str(r.number_norm)) <= last]
    chosen.sort(
        key=lambda r: (
            r.number_sort if r.number_sort is not None else float("inf"),
            r.source_order,
            str(r.id),
        )
    )
    return [str(r.id) for r in chosen]


def _carry_positions(connection: Connection, payload: dict[str, Any], now: datetime) -> int:
    """KOReader takes the volume for a new document and asks where it was: each profile that
    was reading the chapters is given **one** position on the volume, at the furthest chapter it
    has proven (`reading_progress_proven`, as the relay answers), so that the e-reader opens
    where the reader was. The page is the chapter's own when it is exact, the start of the
    chapter when it is a guess, the end of it when the chapter is read. It is a device report:
    it marks nothing read (L14a), and a profile that cannot see the Edition gets nothing."""
    order = [str(unit) for unit, _first, _last in payload["spans"]]
    spans = tuple(
        ChapterSpan(str(unit), rank, int(first), int(last))
        for rank, (unit, first, last) in enumerate(payload["spans"], start=1)
    )
    total = spans[-1].end_page
    old_items = [str(o["export_item_id"]) for o in payload["olds"]]
    position = reading.reading_position
    profiles = connection.execute(
        sa.select(position.c.user_id)
        .where(position.c.carrier_type == "export_item", position.c.carrier_id.in_(old_items))
        .distinct()
        .order_by(position.c.user_id)
    ).scalars()
    carried = 0
    for user_id in list(profiles):
        rows = {
            r.unit_id: r
            for r in progress_of(
                connection, viewer_id=str(user_id), subject_id=str(user_id),
                unit_ids=order, proven=True,
            )
        }  # fmt: skip
        furthest = next(
            (u for u in reversed(order) if u in rows and rows[u].state in _STARTED), None
        )
        if furthest is None:
            continue
        span = next(s for s in spans if s.unit_id == furthest)
        row = rows[furthest]
        page = span.start_page
        if row.state == "read":
            page = span.end_page
        elif row.last_position_id is not None:
            # exact only when the position was on this chapter alone (the view's own flag is
            # computed against the coverage as it is now, which the volume has just changed)
            found = connection.execute(
                sa.select(position.c.locator, position.c.covered_units).where(
                    position.c.id == row.last_position_id
                )
            ).one_or_none()
            if found is not None and list(found.covered_units or []) == [furthest]:
                chapter_page = (found.locator or {}).get("page")
                if isinstance(chapter_page, int) and not isinstance(chapter_page, bool):
                    page = volume_page_of(spans, furthest, chapter_page) or page
        connection.execute(
            sa.insert(position).values(
                id=new_ulid(), user_id=str(user_id), carrier_type="export_item",
                carrier_id=str(payload["export_item_id"]),
                content_hash=str(payload["derived_hash"]),
                locator={"page": page, "carried": True, "device": "Shelfhold"},
                percent=page / total, client_ts=now, server_ts=now, source="device",
                covered_units=order, read_units=[], undone_units={},
            )
        )  # fmt: skip
        carried += 1
    return carried
