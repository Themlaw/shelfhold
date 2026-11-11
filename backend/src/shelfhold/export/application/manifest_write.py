"""Completing the manifest line of a file that was written (02 section 5, the `write` intent).

Shared by the pipeline (right after the atomic placement) and by the boot-time resolution of open
intents (after a crash between the placement and the commit of the manifest).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.application.intents import ManifestHook, OpenIntent
from shelfhold.export.domain.provenance_mirror import MIRROR_DIR
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.fsops.identity import FileIdentity
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.library.public import record_destination_copy

__all__ = [
    "ReservedLine",
    "adopt_written_hook",
    "complete_write",
    "release_reserved_hook",
    "reserved_line",
]

Locate = Callable[[str], tuple[str, str]]


def complete_write(
    connection: Connection,
    fsops: Fsops,
    locate: Locate,
    now: datetime,
    *,
    destination_id: str,
    item_id: str,
    target: str,
    identity: FileIdentity,
) -> None:
    """The file is at `target`: the reserved line becomes `written`, the replica is recorded."""
    record, coverage = export.export_record, export.unit_coverage
    where = (record.c.shelf_destination_id == destination_id, record.c.export_item_id == item_id)
    row = connection.execute(sa.select(record).where(*where)).one()
    unit_id = connection.execute(  # a volume covers several: its replica is told by the first
        sa.select(coverage.c.unit_id)
        .where(coverage.c.carrier_type == "export_item", coverage.c.carrier_id == item_id)
        .order_by(coverage.c.position)
        .limit(1)
    ).scalar_one()
    connection.execute(
        sa.update(record)
        .where(*where)
        .values(
            size=identity.size, mtime_ns=identity.mtime_ns, written_by_shelfhold_at=now,
            exported_at=now, state="written",
        )
    )  # fmt: skip
    root_id, root_rel = locate(target)
    record_destination_copy(
        connection, unit_id=unit_id, export_item_id=item_id, content_hash=row.content_hash,
        file_hash=row.file_hash, physical_root_id=root_id, rel_path=root_rel, size=identity.size,
        mtime_ns=identity.mtime_ns, ino=identity.ino, dev=fsops.device_of(target), verified_at=now,
    )  # fmt: skip


def adopt_written_hook(fsops: Fsops, locate: Locate, clock: Clock) -> ManifestHook:
    """For `IntentJournal.resolve_open(on_done=...)`: a `write` whose expected file is at the
    target gets its reserved manifest line completed; other operations are not ours."""

    def adopt(connection: Connection, intent: OpenIntent) -> None:
        if intent.op != "write" or intent.dst is None or f"/{MIRROR_DIR}/" in intent.dst:
            return  # a provenance mirror file has no manifest line to complete
        record = export.export_record
        for row in connection.execute(
            sa.select(
                record.c.shelf_destination_id, record.c.export_item_id, record.c.rel_path
            ).where(record.c.state == "planned", record.c.file_hash == intent.expected_sha256)
        ):
            if row.rel_path is not None and intent.dst.endswith("/" + row.rel_path):
                observed = fsops.observe(intent.dst)
                if observed is None:
                    raise FileNotFoundError(intent.dst)
                complete_write(
                    connection, fsops, locate, clock.now(), destination_id=row.shelf_destination_id,
                    item_id=row.export_item_id, target=intent.dst, identity=observed[1],
                )  # fmt: skip
                return
        raise LookupError(f"no reserved manifest line for {intent.dst}")

    return adopt


@dataclass(frozen=True)
class ReservedLine:
    """The manifest line a `write` intent reserved: where it is and whose locks guard it."""

    destination_id: str
    item_id: str
    rel_path: str
    work_id: str


def reserved_line(connection: Connection, intent: OpenIntent) -> ReservedLine | None:
    """The `planned` line reserved for this `write` intent (None: a mirror file, another op)."""
    if intent.op != "write" or intent.dst is None or f"/{MIRROR_DIR}/" in intent.dst:
        return None
    record, item, edition = export.export_record, export.export_item, catalog.edition
    for row in connection.execute(
        sa.select(
            record.c.shelf_destination_id, record.c.export_item_id, record.c.rel_path,
            edition.c.work_id,
        )
        .select_from(
            record.join(item, item.c.id == record.c.export_item_id).join(
                edition, edition.c.id == item.c.edition_id
            )
        )
        .where(record.c.state == "planned", record.c.file_hash == intent.expected_sha256)
    ):  # fmt: skip
        if row.rel_path is not None and intent.dst.endswith("/" + row.rel_path):
            return ReservedLine(
                row.shelf_destination_id, row.export_item_id, row.rel_path, row.work_id
            )
    return None


def release_reserved_hook(fsops: Fsops, locate: Locate, clock: Clock) -> ManifestHook:
    """For `IntentJournal.abandon(release=...)`: the intent is given up, its `planned` line must
    not stay an orphan. The disk is looked at, never touched:

    - nothing at the target: the line stays `planned`, the planner takes the unit again;
    - the expected file: it is recognised, the line is completed as after a normal write;
    - another file: the line stays `planned` and the reconciliation reports a foreign writer.
    """

    def release(connection: Connection, intent: OpenIntent) -> None:
        line = reserved_line(connection, intent)
        if line is None or intent.dst is None:
            return
        observed = fsops.observe(intent.dst)
        if observed is None or observed[0] != "file":
            return
        if intent.expected_sha256 is not None and (
            fsops.sha256_of(intent.dst) == intent.expected_sha256
        ):
            complete_write(
                connection, fsops, locate, clock.now(), destination_id=line.destination_id,
                item_id=line.item_id, target=intent.dst, identity=observed[1],
            )  # fmt: skip

    return release
