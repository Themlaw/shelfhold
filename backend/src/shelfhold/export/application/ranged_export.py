"""The export of a file kept under `/data/ranged/` (EF-SRC-032, EF-BIB-040, EF-LEC-014).

M1a files the bytes a `ranged_content` service delivered for a range (`RangedKeeper`); this is
the door by which such a file leaves. In this order, and nowhere else:

1. **Where.** The file is `<data>/ranged/<export_item id>.epub`, found by the id alone (a ULID),
   never by a path that came from elsewhere: no link on the way or at the end, a regular file,
   and the folder the file lives in is the folder of the ranges. Anything else is refused and not
   one byte of it is read.
2. **Trust.** What is kept is what the source sent, so it goes through the neutralizer (EF-BIB-051)
   and only the derivative is ever handed on.
3. **Coverage.** The table of contents of the derivative is compared with the Units the item is
   declared to carry (`verify_item`): a hole, an overlap, a chapter listed twice refuses the export
   with the problems listed, so an incomplete EPUB is never presented as complete.
4. **Policy.** `decide_export` (a volume that is not closed leaves only under `rolling_tail`; a
   whole novel only when it is finished) and, for a file already served, `decide_replacement`: a
   closed volume is never rewritten, and the tail is replaced only through the same-path upgrade
   (L16, the `replace` port) unless a reader finished it or the replacement is suspect (L17).

The placement itself (`place`, `replace`) is a port: what writes to a destination is the export
pipeline and the L16 upgrade, never this module.
"""

from __future__ import annotations

import hashlib
import io
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, cast

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import (
    DEFAULT_MIN_PAGE_RATIO,
    CoverageReport,
    suspect_reason,
    verify_item,
)
from shelfhold.infrastructure.archives import DEFAULT_LIMITS
from shelfhold.infrastructure.content_trust import NeutralizationError, neutralize_epub
from shelfhold.infrastructure.packaging.epub import TocError, read_toc
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.volumes import open_data_folder
from shelfhold.library.public import (
    ExportVerdict,
    ReplaceVerdict,
    decide_export,
    decide_replacement,
    parse_policy,
    received_content_hash,
)

__all__ = [
    "PreparedRange",
    "RangedExportError",
    "RangedExportOutcome",
    "RangedExporter",
    "ServedCopy",
    "locate_ranged",
]

RANGED_DIR: Final = "ranged"
_ULID: Final = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


class RangedExportError(Exception):
    """A refusal, with a stable `code`; nothing was written."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _kept(data_dir: Path, item_id: str, *, read: bool) -> tuple[str, bytes]:
    """(name, bytes) of the kept file of an export item, or a refusal. Nothing of the path comes
    from the caller but the id, which must be a ULID. The folder is opened without following a
    link and the file is read through that descriptor: a swap after the check reads nothing
    outside (no window between the check and the read)."""
    if not _ULID.fullmatch(item_id):
        raise RangedExportError("ranged.bad_id")
    try:
        with open_data_folder(Path(data_dir), RANGED_DIR) as folder:
            names = [n for n in folder.names() if n.startswith(item_id)]
            name = f"{item_id}.epub"
            if name not in names:
                raise RangedExportError("ranged.missing" if not names else "ranged.not_epub")
            if not read:
                return name, b""
            data = folder.read(name, DEFAULT_LIMITS.max_total_bytes)
    except FileNotFoundError:
        raise RangedExportError("ranged.missing") from None
    except ValueError as error:
        code = "ranged.folder_unsafe" if "folder" in str(error) else "ranged.outside_root"
        raise RangedExportError(code, "a link or a special file") from None
    except OSError:
        raise RangedExportError("ranged.folder_unsafe") from None
    if not data:
        raise RangedExportError("ranged.outside_root", "an empty file")
    return name, data


def locate_ranged(data_dir: Path, item_id: str) -> Path:
    """Where the kept file of an export item is (for display); nothing is read."""
    name, _ = _kept(data_dir, item_id, read=False)
    return Path(data_dir) / RANGED_DIR / name


@dataclass(frozen=True)
class ServedCopy:
    """What the destination holds for the item now. `file_hash` is the fingerprint of the kept
    file the served copy was made from (`PreparedRange.source_hash`, the `sidecar_sha256` of the
    manifest line): the file served itself is that one with its OPF edited."""

    file_hash: str
    skip_read: bool = True  # the shelf keeps the files a reader finished (L16)
    spine_count: int | None = None  # None: unknown, the guard then says nothing (L17)


@dataclass(frozen=True)
class PreparedRange:
    item_id: str
    edition_id: str
    path: Path  # the neutralized derivative, in staging
    file_hash: str  # of the derivative
    source_hash: str  # of the file kept under /data/ranged, as the source sent it
    payload_hash: str | None  # the payload of the EPUB, OPF aside: what the anti-echo compares
    spine_count: int
    report: CoverageReport
    closed: bool


@dataclass(frozen=True)
class RangedExportOutcome:
    action: str  # "placed" | "replaced" | "kept" | "waited"
    reason: str | None = None
    warning: str | None = None


class RangedExporter:
    def __init__(
        self,
        engine: Engine,
        data_dir: Path,
        *,
        min_page_ratio: float = DEFAULT_MIN_PAGE_RATIO,
        staging_dir: Path | None = None,
    ) -> None:
        self._engine = engine
        self._data_dir = Path(data_dir)
        self._min_ratio = min_page_ratio
        self._staging = staging_dir or self._data_dir / "tmp"

    # -- preparation ------------------------------------------------------------------------

    @contextmanager
    def prepare(self, item_id: str) -> Iterator[PreparedRange]:
        """The derivative of the kept file, with the verdict of its coverage. Raises
        `RangedExportError` when the file is unsafe or its coverage is not complete."""
        with self._engine.connect() as connection:
            item = connection.execute(
                sa.select(export.export_item.c.edition_id, export.export_item.c.closed).where(
                    export.export_item.c.id == item_id
                )
            ).one_or_none()
            if item is None:
                raise RangedExportError("ranged.unknown_item")
        name, raw = _kept(self._data_dir, item_id, read=True)
        with (
            tempfile.NamedTemporaryFile(
                dir=self._staging if self._staging.is_dir() else None, suffix=".epub", delete=True
            ) as out,
        ):
            try:
                neutralize_epub(io.BytesIO(raw), cast(BinaryIO, out), name=name)
            except NeutralizationError as error:
                raise RangedExportError("ranged.untrusted_entry", error.reason) from None
            out.flush()
            derivative = Path(out.name)
            try:
                toc = read_toc(derivative)
            except TocError as error:
                raise RangedExportError("ranged.no_toc", error.reason) from None
            with self._engine.connect() as connection:
                report = verify_item(connection, item_id, toc)
            source_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
            digest = hashlib.sha256()
            with derivative.open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
            yield PreparedRange(
                item_id, str(item.edition_id), derivative, "sha256:" + digest.hexdigest(),
                source_hash, received_content_hash(derivative, ".epub"), len(toc.spine), report,
                bool(item.closed),
            )  # fmt: skip

    # -- facts ------------------------------------------------------------------------------

    def source_hash(self, item_id: str) -> str | None:
        """The fingerprint of the file kept for the item, None when it is missing or unsafe (the
        planner reads it to know whether the kept file moved since the copy served)."""
        try:
            _, raw = _kept(self._data_dir, item_id, read=True)
        except RangedExportError:
            return None
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def served_copy(self, destination_id: str, item_id: str) -> ServedCopy | None:
        """The file the manifest says the destination holds for the item, or None."""
        record, shelf, dest = export.export_record, export.shelf, export.shelf_destination
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(record.c.sidecar_sha256, record.c.spine_count, shelf.c.skip_read_upgrades)
                .select_from(
                    record.join(dest, dest.c.id == record.c.shelf_destination_id).join(
                        shelf, shelf.c.id == dest.c.shelf_id
                    )
                )
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == item_id,
                    record.c.state == "written",
                    record.c.sidecar_sha256.is_not(None),
                )
            ).one_or_none()
        if row is None:
            return None
        return ServedCopy(
            str(row.sidecar_sha256), bool(row.skip_read_upgrades),
            None if row.spine_count is None else int(row.spine_count),
        )  # fmt: skip

    def note_placed(self, destination_id: str, item_id: str, prepared: PreparedRange) -> None:
        """Write down the length of the file placed: what the guard of the next replacement
        compares with. The two doors call it once the file is on the destination."""
        record = export.export_record
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(record)
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == item_id,
                )
                .values(spine_count=prepared.spine_count)
            )

    def _stubbed(self, connection: Connection, item_id: str) -> bool:
        """A Unit the item carries is only a stub upstream (EF-BIB-024)."""
        return (
            connection.execute(
                sa.text(
                    "SELECT 1 FROM unit_coverage c JOIN unit u ON u.id = c.unit_id "
                    "WHERE c.carrier_type = 'export_item' AND c.carrier_id = :i "
                    "AND u.availability = 'stubbed' LIMIT 1"
                ),
                {"i": item_id},
            ).first()
            is not None
        )

    def _facts(
        self, connection: Connection, edition_id: str, item_id: str
    ) -> tuple[str, bool, bool]:
        edition = catalog.edition
        row = connection.execute(
            sa.select(edition.c.volume_policy, edition.c.upstream_status).where(
                edition.c.id == edition_id
            )
        ).one()
        read = connection.execute(
            sa.text(
                "SELECT 1 FROM reading_progress p WHERE p.state = 'read' AND p.unit_id IN ("
                "SELECT unit_id FROM unit_coverage "
                "WHERE carrier_type = 'export_item' AND carrier_id = :i "
                "UNION "  # the chapters a re-cut chapter of the volume became count too
                "SELECT c2.unit_id FROM unit_coverage c1 JOIN unit_coverage c2 "
                "ON c2.carrier_type = 'unit' AND c2.carrier_id = c1.unit_id "
                "WHERE c1.carrier_type = 'export_item' AND c1.carrier_id = :i) LIMIT 1"
            ),
            {"i": item_id},
        ).first()
        return str(row.volume_policy), row.upstream_status == "finished", read is not None

    # -- the export -------------------------------------------------------------------------

    def export(
        self,
        item_id: str,
        *,
        served: ServedCopy | None,
        place: Callable[[PreparedRange], None],
        replace: Callable[[PreparedRange], None],
    ) -> RangedExportOutcome:
        """Export the kept file of `item_id`: `place` for a first write, `replace` (the L16
        upgrade) for the tail of a volume already served. Otherwise nothing is written."""
        with self._engine.connect() as connection:
            edition_id = connection.execute(
                sa.select(export.export_item.c.edition_id).where(export.export_item.c.id == item_id)
            ).scalar()
            if edition_id is None:
                raise RangedExportError("ranged.unknown_item")
            raw_policy, finished, read = self._facts(connection, str(edition_id), item_id)
            stubbed = self._stubbed(connection, item_id)
            closed = bool(
                connection.execute(
                    sa.select(export.export_item.c.closed).where(export.export_item.c.id == item_id)
                ).scalar()
            )
        policy = parse_policy(raw_policy)
        verdict = decide_export(policy, volume_closed=closed, novel_finished=finished)
        if verdict.verdict is ExportVerdict.REFUSE:
            raise RangedExportError("ranged.policy_refuses", verdict.reason or "")
        if verdict.verdict is ExportVerdict.WAIT:
            if served is not None and not closed:  # nothing rolls the tail under this policy
                return RangedExportOutcome("kept", "policy_keeps_served")
            return RangedExportOutcome("waited", verdict.reason)
        if served is not None and closed:  # frozen: not even read
            return RangedExportOutcome("kept", "volume_closed")
        with self.prepare(item_id) as prepared:
            if not prepared.report.complete:
                raise RangedExportError(
                    "ranged.coverage_incomplete", ",".join(prepared.report.problems)
                )
            if served is None:
                place(prepared)
                return RangedExportOutcome("placed", warning=verdict.warning)
            if served.file_hash == prepared.source_hash:
                return RangedExportOutcome("kept", "up_to_date")
            suspect = (
                suspect_reason(
                    stored_pages=served.spine_count, new_pages=prepared.spine_count,
                    availability="stubbed" if stubbed else "free", min_ratio=self._min_ratio,
                )
                is not None
            )  # fmt: skip
            decision = decide_replacement(
                policy, volume_closed=closed, served=True, read=read,
                skip_read=served.skip_read, suspect=suspect,
            )  # fmt: skip
            if decision.verdict is ReplaceVerdict.REPLACE:
                replace(prepared)
                return RangedExportOutcome("replaced", warning=decision.warning)
            if decision.verdict is ReplaceVerdict.REFUSE_SUSPECT:
                raise RangedExportError("ranged.suspect_revision")
            return RangedExportOutcome("kept", decision.verdict.value)
