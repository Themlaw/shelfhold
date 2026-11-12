"""Same-path upgrade of an exported file (EF-EXP-016, under the intent journal of EF-EXP-041).

A better version of a Unit already exported (a better copy, the preferred team's, a reacquisition
after an integrity failure) replaces the served file **at its path**: Kavita sees the same file
name, so the chapter, its progress and its place in the series stay. The old version goes to the
bin of its root (`.shelfhold/recycle/<intent>/<path>`, 30 days) and `restore` puts it back.

The replacement is one `upgrade` intent, recorded and marked `applying` before the disk is
touched, then three steps (`Fsops.same_path_stage`, `same_path_swap`, `same_path_recycle_old`),
then the manifest, the registry of copies, the bin entry, the `export_upgrade` row, the event and
`done` in one transaction. A SIGKILL between any two steps leaves a state the boot settles
(`IntentJournal._resolve_upgrade` with `settle_hook`): the path is never empty and never holds
two files, and the old version is never lost.

Before anything is recorded, in this order:

- the root is frozen (`FreezeState`, EF-EXP-064): nothing, not even the intent;
- the Unit was read and the shelf says "do not upgrade read Units" (`skip_read_upgrades`, on by
  default): nothing, whatever the reason (every path that asks for an upgrade comes here);
- nothing changed (same content): nothing;
- the better version comes from a revision the suspect-revision guard quarantined (EF-BIB-024):
  nothing, unless the request carries the grant of the reserved action `revisions.force`;
- the Unit is irreplaceable (EF-BIB-031): its identifier must be typed, which an automatic
  upgrade cannot do;
- the served file is not the one the manifest knows (a third party rewrote it): nothing.

A new file at the same path is a new document for KOReader (its identity is a partial MD5 of the
file): every upgrade says so with `unit.upgraded` and `koreader_reset = true` (D21), listed by
`status`; the old hash stays resolvable through `document_alias`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import DEFAULT_MIN_PAGE_RATIO, suspect_reason
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.export.application.intents import (
    IntentJournal,
    ManifestHook,
    OpenIntent,
    open_intent_paths,
)
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.mirror import ProvenanceMirror
from shelfhold.export.application.rendering import CbzRenderer, comicinfo_xml
from shelfhold.export.application.snapshot import (
    DestinationContext,
    barrier_closed_by,
    load_context,
)
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.identity.public import Allowed
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import FileIdentity, Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import escaped
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.schema.export import UPGRADE_REASONS as REASONS
from shelfhold.library.public import (
    RootLocator,
    binned_export_file,
    confirmation_phrase,
    irreplaceability,
    record_binned_export_file,
    record_destination_copy,
    upgrade_source,
)

__all__ = [
    "UPGRADE_REASONS",
    "SamePathUpgrade",
    "UpgradeOutcome",
    "UpgradeRequest",
    "UpgradeResult",
]

log = structlog.get_logger(__name__)

# What a request may say; `restore` is the other door (`SamePathUpgrade.restore`).
UPGRADE_REASONS: frozenset[str] = frozenset(REASONS) - {"restore"}
EVENT = "unit.upgraded"
PAYLOAD_KIND = "same_path_upgrade"


class UpgradeOutcome(enum.Enum):
    UPGRADED = "upgraded"
    UP_TO_DATE = "up_to_date"  # the content asked for is the one served
    SKIPPED_READ = "skipped_read"  # read, and the shelf does not upgrade read Units
    NEEDS_CONFIRMATION = "needs_confirmation"  # irreplaceable: the identifier was not typed
    ROOT_FROZEN = "root_frozen"  # the root (or the instance) is frozen: nothing is written
    BUSY = "busy"  # another intent holds the path
    TARGET_CHANGED = "target_changed"  # the served file is not the manifest's: left alone
    SUSPECT_REVISION = "suspect_revision"  # quarantined by the guard: never promoted (EF-BIB-024)
    REFUSED_AGE = "refused_age"  # the shelf below 18+ no longer admits the Edition (EF-BIB-037)
    # the destination is paused, read-only, or not known to partition: nothing is written there
    REFUSED_DESTINATION = "refused_destination"


@dataclass(frozen=True)
class UpgradeRequest:
    destination_id: str
    export_item_id: str
    content_id: str  # the better version: a `content` of the same Unit
    reason: str  # one of UPGRADE_REASONS
    automatic: bool = True  # asked by Shelfhold itself (a follow cycle, an integrity check)
    typed: str | None = None  # the Unit's identifier, typed, for an irreplaceable Unit
    job_id: str | None = None  # holds the locks of the Work and the series folder when given
    # The grant of `revisions.force`, for a version the guard quarantined: nothing else lifts it
    suspect_grant: Allowed | None = None


@dataclass(frozen=True)
class UpgradeResult:
    outcome: UpgradeOutcome
    upgrade_id: str | None = None


@dataclass(frozen=True)
class _Served:
    """What the manifest says is served for one export item of one destination."""

    ctx: DestinationContext
    export_item_id: str
    unit_id: str  # the first Unit the item covers (a volume covers several)
    unit_ids: tuple[str, ...]
    work_id: str
    rel_path: str
    file_hash: str
    content_hash: str | None
    content_id: str | None
    series_name: str
    skip_read: bool

    @property
    def target(self) -> str:
        return self.ctx.local(self.rel_path)

    @property
    def folder(self) -> str:
        return self.rel_path.split("/", 1)[0]


@dataclass(frozen=True)
class _New:
    """The version that takes the path: its bytes and what the manifest will say of it."""

    data: BinaryIO
    file_hash: str
    content_hash: str | None
    sidecar_sha256: str | None
    content_id: str | None
    spine_count: int | None = None  # reading-order items of an EPUB (L17), None for a CBZ


@dataclass(frozen=True)
class UpgradeMark(FileIdentity):
    """`fs_intent.expected_file_id` of an `upgrade`: the identity of the old file, and what the
    transaction that closes the intent needs (also after a crash, from the journal alone)."""

    old_sha256: str = ""
    fields: tuple[tuple[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {**super().to_json(), "old_sha256": self.old_sha256, **dict(self.fields)}


def _is_read(connection: Connection, unit_ids: Sequence[str]) -> bool:
    """Any reader finished one of these Units (`reading_progress`, EF-LEC-014)."""
    return any(
        connection.execute(
            sa.text("SELECT 1 FROM reading_progress WHERE unit_id = :u AND state = 'read' LIMIT 1"),
            {"u": unit_id},
        ).first()
        is not None
        for unit_id in unit_ids
    )


def _quarantined(connection: Connection, variant_id: str | None) -> bool:
    """The latest revision of the Variant was quarantined by the suspect-revision guard."""
    if variant_id is None:
        return False
    return bool(
        connection.execute(
            sa.text(
                "SELECT quarantined_at IS NOT NULL FROM variant_revision "
                "WHERE variant_id = :v ORDER BY revision DESC LIMIT 1"
            ),
            {"v": variant_id},
        ).scalar()
    )


def _served(connection: Connection, destination_id: str, export_item_id: str) -> _Served | None:
    record, item, coverage = export.export_record, export.export_item, export.unit_coverage
    d, shelf = export.shelf_destination, export.shelf
    row = connection.execute(
        sa.select(
            record.c.rel_path,
            record.c.file_hash,
            record.c.content_hash,
            record.c.state,
            record.c.locator_type,
            item.c.content_id,
            item.c.frozen_attrs,
            shelf.c.skip_read_upgrades,
        )
        .select_from(
            record.join(item, item.c.id == record.c.export_item_id)
            .join(d, d.c.id == record.c.shelf_destination_id)
            .join(shelf, shelf.c.id == d.c.shelf_id)
        )
        .where(
            record.c.shelf_destination_id == destination_id,
            record.c.export_item_id == export_item_id,
        )
    ).one_or_none()
    if row is None or row.locator_type != "path" or row.state != "written" or not row.file_hash:
        return None
    unit_ids = tuple(
        connection.execute(
            sa.select(coverage.c.unit_id)
            .where(
                coverage.c.carrier_type == "export_item", coverage.c.carrier_id == export_item_id
            )
            .order_by(coverage.c.position)
        ).scalars()
    )
    if not unit_ids:
        return None  # an item that covers nothing has nothing to upgrade
    work_id = connection.execute(
        sa.text(
            "SELECT e.work_id FROM unit u JOIN edition e ON e.id = u.edition_id WHERE u.id = :u"
        ),
        {"u": unit_ids[0]},
    ).scalar_one()
    return _Served(
        load_context(connection, destination_id), export_item_id, str(unit_ids[0]),
        tuple(map(str, unit_ids)), str(work_id), str(row.rel_path), str(row.file_hash),
        row.content_hash, row.content_id,
        str(dict(row.frozen_attrs).get("series", "")), bool(row.skip_read_upgrades),
    )  # fmt: skip


class SamePathUpgrade:
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
        min_page_ratio: float = DEFAULT_MIN_PAGE_RATIO,
    ) -> None:
        self._min_page_ratio = min_page_ratio
        self._engine = engine
        self._fsops: Fsops = fsops
        self._locate = locate
        self._clock = clock
        self._bus = bus or EventBus(clock)
        self._renderer = renderer or CbzRenderer(fsops, staging_dir=data_dir / "tmp")
        self._mirror = mirror or ProvenanceMirror(engine, fsops, clock=clock)
        self._request_scan = request_scan
        self._journal = IntentJournal(engine, fsops, clock=clock)
        self._locks = ResourceLocks(engine, clock=clock)

    # -- the two doors ----------------------------------------------------------------------

    def upgrade(self, request: UpgradeRequest) -> UpgradeResult:
        """Replace the served file of `request.export_item_id` by `request.content_id`."""
        if request.reason not in UPGRADE_REASONS:
            raise ValueError(f"not an upgrade reason: {request.reason!r}")
        with self._engine.connect() as connection:
            served = _served(connection, request.destination_id, request.export_item_id)
            if served is None:
                raise LookupError(f"no file served for {request.export_item_id}")
            if len(served.unit_ids) != 1:  # a volume is rewritten whole, by `replace_volume`
                raise LookupError(f"{request.export_item_id} covers several Units: not a chapter")
            refused = self._refusal(connection, served, request.typed, check_read=True)
            if refused is not None:
                return UpgradeResult(refused)
            source = upgrade_source(connection, served.unit_id, request.content_id)
            if source is None or source.local_path is None:
                raise LookupError(f"no library copy of content {request.content_id}")
            if source.content_hash is not None and source.content_hash == served.content_hash:
                return UpgradeResult(UpgradeOutcome.UP_TO_DATE)
            if (
                _quarantined(connection, source.variant_id)
                or self._too_short(connection, served, source.page_count)
            ) and not isinstance(request.suspect_grant, Allowed):
                log.warning("upgrade.suspect_revision", unit_id=served.unit_id)
                return UpgradeResult(UpgradeOutcome.SUSPECT_REVISION)
            xml = comicinfo_xml(connection, served.unit_id, served.series_name)
        rendered = self._renderer.render(str(source.local_path), xml)
        try:
            if source.content_hash is not None and rendered.content_hash != source.content_hash:
                raise LookupError(f"{request.content_id}: the archive is not the content's")
            if rendered.file_hash == served.file_hash:
                return UpgradeResult(UpgradeOutcome.UP_TO_DATE)
            new = _New(
                rendered.file, rendered.file_hash, rendered.content_hash, rendered.sidecar_sha256,
                source.content_id,
            )  # fmt: skip
            return self._replace(
                served, new, reason=request.reason, automatic=request.automatic,
                job_id=request.job_id,
            )  # fmt: skip
        finally:
            rendered.file.close()

    def replace_volume(
        self,
        destination_id: str,
        export_item_id: str,
        *,
        data: BinaryIO,
        file_hash: str,
        content_hash: str,
        source_hash: str,
        spine_count: int,
    ) -> UpgradeResult:
        """The tail of a volume (an EPUB of a range, EF-BIB-044) replaced at its path by the new
        file, which the caller has rendered: the same guards as a chapter (root frozen, a Unit a
        reader finished, an irreplaceable Unit, a file someone else rewrote), the same intent, bin
        and manifest. The caller holds the locks of the Work and the folder, as the export
        pipeline does from its last look to its write: they are not taken twice. The length of
        the new file is kept for the guard of the next replacement (L17)."""
        with self._engine.connect() as connection:
            served = _served(connection, destination_id, export_item_id)
            if served is None:
                raise LookupError(f"no file served for {export_item_id}")
            refused = self._refusal(connection, served, None, check_read=True)
            if refused is not None:
                return UpgradeResult(refused)
            stored = connection.execute(
                sa.select(export.export_record.c.spine_count).where(
                    export.export_record.c.shelf_destination_id == destination_id,
                    export.export_record.c.export_item_id == export_item_id,
                )
            ).scalar()
        if file_hash == served.file_hash:
            return UpgradeResult(UpgradeOutcome.UP_TO_DATE)
        if stored is None:  # the length served is unknown: nothing to compare the new one with
            return UpgradeResult(UpgradeOutcome.SUSPECT_REVISION)
        if (
            suspect_reason(
                stored_pages=int(stored),
                new_pages=spine_count,
                availability="free",
                min_ratio=self._min_page_ratio,
            )
            is not None
        ):  # the guard of L17, here too: no way to ask for the replacement skips it
            log.warning("upgrade.suspect_revision", unit_id=served.unit_id)
            return UpgradeResult(UpgradeOutcome.SUSPECT_REVISION)
        new = _New(data, file_hash, content_hash, source_hash, None, spine_count)
        return self._replace_held(
            served, new, reason="better_version", automatic=True, restores=None
        )

    def upgrade_unit(self, unit_id: str, content_id: str) -> list[UpgradeResult]:
        """The door of a better copy received for a Unit (EF-SRC-037, a better rank listed late):
        every file served for the Unit, in every destination, goes through `upgrade` and so
        through every guard of it (read Unit, suspect revision, irreplaceable Unit, frozen root).
        A Unit nothing was exported for has nothing to upgrade."""
        record, coverage = export.export_record, export.unit_coverage
        item, covers = export.export_item, sa.alias(export.unit_coverage)
        with self._engine.connect() as connection:
            served = connection.execute(
                sa.select(record.c.shelf_destination_id, record.c.export_item_id)
                .select_from(
                    record.join(
                        coverage,
                        sa.and_(
                            coverage.c.carrier_type == "export_item",
                            coverage.c.carrier_id == record.c.export_item_id,
                        ),
                    )
                )
                .join(item, item.c.id == record.c.export_item_id)
                .where(
                    coverage.c.unit_id == unit_id,
                    record.c.locator_type == "path",
                    record.c.state == "written",
                    item.c.kind == "unit",  # never a volume or a bundle: a chapter would replace it
                    sa.select(sa.func.count())
                    .select_from(covers)
                    .where(
                        covers.c.carrier_type == "export_item",
                        covers.c.carrier_id == record.c.export_item_id,
                    )
                    .scalar_subquery()
                    == 1,  # it covers this Unit and no other
                )
                .order_by(record.c.shelf_destination_id, record.c.export_item_id)
            ).all()
        return [
            self.upgrade(
                UpgradeRequest(
                    row.shelf_destination_id, row.export_item_id, content_id, "better_version"
                )
            )
            for row in served
        ]

    def _too_short(self, connection: Connection, served: _Served, new_pages: int | None) -> bool:
        """The better version has too few pages next to the one served (EF-BIB-024): checked
        here too, on the page count recorded at reception, so no way to ask for an upgrade
        skips the guard."""
        if served.content_id is None:
            return False
        stored = connection.execute(
            sa.text("SELECT page_count FROM content WHERE id = :c"), {"c": served.content_id}
        ).scalar()
        return (
            suspect_reason(
                stored_pages=stored, new_pages=new_pages, availability="free",
                min_ratio=self._min_page_ratio,
            )
            is not None
        )  # fmt: skip

    def restore(self, upgrade_id: str, *, typed: str | None = None) -> UpgradeResult:
        """Put back the version an upgrade sent to the bin: the same replacement, the other way
        (the version served now goes to the bin in its turn). Asked by a person, so the "read"
        option does not apply; the irreplaceable rule and the frozen root do."""
        table = export.export_upgrade
        with self._engine.connect() as connection:
            row = connection.execute(sa.select(table).where(table.c.id == upgrade_id)).one()
            if row.state != "done" or row.trash_item_id is None:
                raise LookupError(f"upgrade {upgrade_id}: nothing in the bin to put back")
            binned = binned_export_file(connection, row.trash_item_id)
            served = _served(connection, row.shelf_destination_id, row.export_item_id)
            if binned is None or served is None:
                raise LookupError(f"upgrade {upgrade_id}: the bin entry or the file is gone")
            if served.file_hash != row.new_file_hash:
                return UpgradeResult(UpgradeOutcome.TARGET_CHANGED)
            refused = self._refusal(connection, served, typed, check_read=False)
            if refused is not None:
                return UpgradeResult(refused)
        # the bin path as recorded: the file may have been renamed since the upgrade
        with self._fsops.open_bin_entry(served.target, binned.path) as stream:
            new = _New(
                stream, str(row.old_file_hash), row.old_content_hash, row.old_sidecar_sha256,
                row.old_content_id,
            )  # fmt: skip
            return self._replace(
                served, new, reason="restore", automatic=False, restores=upgrade_id
            )

    def settle_hook(self) -> ManifestHook:
        """For `IntentJournal.resolve_open(on_done=...)` at boot: the bookkeeping of an `upgrade`
        intent the replay finished. Other operations are not ours."""

        def settle(connection: Connection, intent: OpenIntent) -> None:
            if intent.op != "upgrade" or intent.payload.get("kind") != PAYLOAD_KIND:
                return
            self._complete(connection, intent.id, intent.payload)

        return settle

    # -- the checks -------------------------------------------------------------------------

    def _refusal(
        self, connection: Connection, served: _Served, typed: str | None, *, check_read: bool
    ) -> UpgradeOutcome | None:
        target = served.target
        root_id, _rel = self._locate(target)
        if (
            self._fsops.frozen
            or self._fsops.freeze_state.frozen_for(root_id)
            or self._fsops.status(target).state is not RootState.OK
        ):  # frozen, or blocked by its sentinel or its lease: nothing, not even the intent
            log.warning("upgrade.root_frozen", root_id=root_id)
            return UpgradeOutcome.ROOT_FROZEN
        closed = self._barrier(connection, served)
        if closed is not None:
            log.info("upgrade.refused_barrier", unit_id=served.unit_id, why=closed)
            return (
                UpgradeOutcome.REFUSED_DESTINATION
                if closed == "destination"
                else UpgradeOutcome.REFUSED_AGE
            )
        if check_read and served.skip_read and _is_read(connection, served.unit_ids):
            log.info("upgrade.skipped_read", unit_id=served.unit_id)
            return UpgradeOutcome.SKIPPED_READ
        for unit_id in served.unit_ids:  # the identifier typed is the first Unit's
            if irreplaceability(connection, unit_id).irreplaceable and (
                typed is None or typed.strip() != confirmation_phrase(served.unit_id)
            ):
                log.info("upgrade.needs_confirmation", unit_id=unit_id)
                return UpgradeOutcome.NEEDS_CONFIRMATION
        if target in open_intent_paths(connection):
            return UpgradeOutcome.BUSY
        return None

    @staticmethod
    def _barrier(connection: Connection, served: _Served) -> str | None:
        """The barrier of the export, read again now: a file already served is not replaced when
        the destination is closed (paused, read-only) or the shelf below 18+ no longer admits its
        Edition (a suggested age above the rating, a rating raised, an Edition withdrawn). The
        very look the planner takes before a write; the destination is told before the age."""
        unit = catalog.unit
        editions = connection.execute(
            sa.select(unit.c.edition_id).where(unit.c.id.in_(served.unit_ids)).distinct()
        ).scalars()
        verdicts = [
            barrier_closed_by(connection, served.ctx.destination_id, edition)
            for edition in editions
        ]
        if "destination" in verdicts:
            return "destination"
        return "age" if "age" in verdicts else None

    # -- the replacement --------------------------------------------------------------------

    def _replace(
        self,
        served: _Served,
        new: _New,
        *,
        reason: str,
        automatic: bool,
        job_id: str | None = None,
        restores: str | None = None,
    ) -> UpgradeResult:
        with self._locks.hold(
            job_id or new_ulid(), work=served.work_id,
            series=f"{served.ctx.destination_id}/{served.folder}", timeout=30.0,
        ):  # fmt: skip
            return self._replace_held(served, new, reason, automatic, restores)

    def _replace_held(
        self, served: _Served, new: _New, reason: str, automatic: bool, restores: str | None
    ) -> UpgradeResult:
        target = served.target
        found = self._fsops.hash_and_identity(target)
        if found is None or found[0] != served.file_hash:
            log.warning("upgrade.target_changed", path=escaped(target))
            return UpgradeResult(UpgradeOutcome.TARGET_CHANGED)
        old_sha, old_identity, _dev = found
        mark = UpgradeMark(
            old_identity.ino, old_identity.size, old_identity.mtime_ns, old_sha256=old_sha,
            fields=(
                ("kind", PAYLOAD_KIND), ("destination_id", served.ctx.destination_id),
                ("export_item_id", served.export_item_id),
                ("content_id", new.content_id), ("content_hash", new.content_hash),
                ("sidecar_sha256", new.sidecar_sha256), ("reason", reason),
                ("automatic", automatic), ("restores", restores),
                ("spine_count", new.spine_count),
            ),
        )  # fmt: skip
        intent = self._journal.record(
            op="upgrade", src=target, dst=target, expected_sha256=new.file_hash,
            expected_file_id=mark,
        )  # fmt: skip
        self._journal.mark_applying(intent)
        try:
            self._fsops.same_path_stage(
                target, intent, new.data,
                verify_digest=lambda: new.file_hash.removeprefix("sha256:"),
            )  # fmt: skip
        except BaseException:
            # the served file was not touched: what was staged goes, the intent is closed
            try:
                self._fsops.same_path_discard(target, intent)
            finally:
                self._journal.cancel(intent)
            raise
        # From here an error leaves the intent open: the boot (or `intents resolve`) settles it.
        self._fsops.same_path_swap(target, intent)
        self._fsops.same_path_recycle_old(target, intent)
        with self._engine.begin() as connection:
            upgrade_id, event = self._complete(connection, intent, mark.to_json())
            self._journal.mark_done(intent, within=connection)
        if event is not None:
            self._bus.deliver([event])
        self._after(served)
        log.info("upgrade.done", unit_id=served.unit_id, reason=reason)
        return UpgradeResult(UpgradeOutcome.UPGRADED, upgrade_id)

    def _after(self, served: _Served) -> None:
        """The provenance mirror of the folder, then Kavita is asked to look at it again; the
        file is placed and committed, so a failure here is healed by the next run."""
        try:
            self._mirror.sync_destination(served.ctx.destination_id, folder=served.folder)
        except Exception:
            log.warning("mirror_write_failed", destination=served.ctx.destination_id)
        if self._request_scan is not None:
            self._request_scan(served.ctx.local(served.folder))

    # -- the bookkeeping, in the transaction that closes the intent -------------------------

    def _complete(
        self, connection: Connection, intent_id: str, payload: dict[str, Any]
    ) -> tuple[str, PublishedEvent | None]:
        """The new file is at the path and the old one in the bin: the manifest line, the
        export item, the registry of copies, the aliases, the bin entry, the `export_upgrade`
        row and `unit.upgraded`. Runs once per intent (the journal closes it in the same
        transaction); a second call finds the row and does nothing."""
        table = export.export_upgrade
        existing = connection.execute(
            sa.select(table.c.id).where(table.c.intent_id == intent_id)
        ).scalar()
        if existing is not None:
            return str(existing), None
        record, item = export.export_record, export.export_item
        intent = connection.execute(
            sa.text("SELECT dst_path, expected_sha256 FROM fs_intent WHERE id = :i"),
            {"i": intent_id},
        ).one_or_none()
        if intent is None:
            raise LookupError(f"upgrade intent {intent_id} is gone")
        target, new_hash = str(intent.dst_path), str(intent.expected_sha256)
        destination_id = str(payload["destination_id"])
        item_id = str(payload["export_item_id"])
        line = connection.execute(
            sa.select(record, item.c.content_id)
            .join(item, item.c.id == record.c.export_item_id)
            .where(record.c.shelf_destination_id == destination_id, item.c.id == item_id)
        ).one_or_none()
        if line is None:  # the line was purged in between: a drift for an admin, not a crash
            raise LookupError(f"export line {destination_id}/{item_id} is gone")
        unit_id = connection.execute(
            sa.select(export.unit_coverage.c.unit_id)
            .where(
                export.unit_coverage.c.carrier_type == "export_item",
                export.unit_coverage.c.carrier_id == item_id,
            )
            .order_by(export.unit_coverage.c.position)
            .limit(1)
        ).scalar_one_or_none()
        if unit_id is None:
            raise LookupError(f"export item {item_id} covers no Unit any more")
        now = self._clock.now()
        observed = self._fsops.observe(target)
        if observed is None:
            raise FileNotFoundError(target)
        identity = observed[1]
        connection.execute(
            sa.update(record)
            .where(record.c.shelf_destination_id == destination_id,
                   record.c.export_item_id == item_id)
            .values(
                file_hash=new_hash, content_hash=payload.get("content_hash"),
                sidecar_sha256=payload.get("sidecar_sha256"), size=identity.size,
                mtime_ns=identity.mtime_ns, written_by_shelfhold_at=now, exported_at=now,
                updated_at=now, state="written", spine_count=payload.get("spine_count"),
            )
        )  # fmt: skip
        connection.execute(
            sa.update(item).where(item.c.id == item_id).values(content_id=payload.get("content_id"))
        )
        root_id, root_rel = self._locate(target)
        record_destination_copy(
            connection, unit_id=unit_id, export_item_id=item_id,
            content_hash=str(payload.get("content_hash") or line.content_hash),
            file_hash=new_hash, physical_root_id=root_id, rel_path=root_rel, size=identity.size,
            mtime_ns=identity.mtime_ns, ino=identity.ino, dev=self._fsops.device_of(target),
            verified_at=now,
        )  # fmt: skip
        old_hash = str(payload["old_sha256"])
        self._alias(connection, item_id, old_hash, line.content_hash)
        self._alias(connection, item_id, new_hash, payload.get("content_hash"))
        _local, bin_path = self._fsops.same_path_bin_entry(target, intent_id)
        trash_id = record_binned_export_file(
            connection, export_item_id=item_id, physical_root_id=root_id, path=bin_path,
            size=int(payload.get("size") or 0) or None, now=now,
        )  # fmt: skip
        upgrade_id = new_ulid()
        reason, automatic = str(payload["reason"]), bool(payload.get("automatic"))
        connection.execute(
            sa.insert(table).values(
                id=upgrade_id, intent_id=intent_id, shelf_destination_id=destination_id,
                export_item_id=item_id, unit_id=unit_id, reason=reason, automatic=automatic,
                koreader_reset=True, old_content_id=line.content_id,
                old_content_hash=line.content_hash, old_sidecar_sha256=line.sidecar_sha256,
                old_file_hash=old_hash, new_file_hash=new_hash, trash_item_id=trash_id,
                created_at=now,
            )
        )  # fmt: skip
        restores = payload.get("restores")
        if restores:
            connection.execute(
                sa.update(table)
                .where(table.c.id == restores, table.c.state == "done")
                .values(state="restored", restored_at=now)
            )
        event = self._bus.publish(
            connection, EVENT,
            {
                "unit_id": unit_id, "export_item_id": item_id,
                "shelf_destination_id": destination_id, "upgrade_id": upgrade_id,
                "reason": reason, "automatic": automatic, "koreader_reset": True,
            },
            visibility="ops",  # notifiable (D21); the Activity screen comes in M3
        )  # fmt: skip
        return upgrade_id, event

    @staticmethod
    def _alias(
        connection: Connection, item_id: str, file_hash: str, content_hash: str | None
    ) -> None:
        """A hash of this item's file resolves it (EF-LEC-009): a KOReader position taken on
        the old file is still found after the upgrade."""
        if content_hash is None:
            return
        alias = export.document_alias
        found = connection.execute(
            sa.select(alias.c.file_hash).where(
                alias.c.export_item_id == item_id, alias.c.file_hash == file_hash
            )
        ).first()
        if found is None:
            connection.execute(
                sa.insert(alias).values(
                    export_item_id=item_id, file_hash=file_hash, content_hash=content_hash
                )
            )
