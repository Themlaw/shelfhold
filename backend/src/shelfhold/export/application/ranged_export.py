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
import os
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
from shelfhold.infrastructure.volumes import read_data_file
from shelfhold.library.public import (
    ExportVerdict,
    ReplaceVerdict,
    decide_export,
    decide_replacement,
    parse_policy,
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


def locate_ranged(data_dir: Path, item_id: str) -> Path:
    """The kept file of an export item, or a refusal. Nothing of the path comes from the caller
    but the id, which must be a ULID; links are never followed."""
    if not _ULID.fullmatch(item_id):
        raise RangedExportError("ranged.bad_id")
    folder = Path(data_dir) / RANGED_DIR
    try:
        if folder.is_symlink() or not folder.is_dir():
            raise RangedExportError("ranged.folder_unsafe")
        names = [e.name for e in os.scandir(folder) if e.name.startswith(item_id)]
    except OSError:
        raise RangedExportError("ranged.folder_unsafe") from None
    found = [n for n in names if n == f"{item_id}.epub"]
    if not found:
        raise RangedExportError("ranged.missing" if not names else "ranged.not_epub")
    path = folder / found[0]
    if path.resolve().parent != folder.resolve():
        raise RangedExportError("ranged.outside_root")
    return path


def _read_regular(path: Path) -> bytes:
    """The bytes of the file, never through a link, never more than an archive may hold; a link,
    a special file or an empty one comes back as a refusal."""
    try:
        data = read_data_file(path, DEFAULT_LIMITS.max_total_bytes)
    except OSError:
        raise RangedExportError("ranged.missing") from None
    if not data:
        raise RangedExportError("ranged.outside_root", "a link, a special file or an empty one")
    return data


@dataclass(frozen=True)
class ServedCopy:
    """What the destination holds for the item now."""

    file_hash: str
    skip_read: bool = True  # the shelf keeps the files a reader finished (L16)
    spine_count: int | None = None  # None: unknown, the guard then says nothing (L17)


@dataclass(frozen=True)
class PreparedRange:
    item_id: str
    edition_id: str
    path: Path  # the neutralized derivative, in staging
    file_hash: str
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
        path = locate_ranged(self._data_dir, item_id)
        with self._engine.connect() as connection:
            item = connection.execute(
                sa.select(export.export_item.c.edition_id, export.export_item.c.closed).where(
                    export.export_item.c.id == item_id
                )
            ).one_or_none()
            if item is None:
                raise RangedExportError("ranged.unknown_item")
        raw = _read_regular(path)
        self._staging.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.NamedTemporaryFile(dir=self._staging, suffix=".epub", delete=True) as out,
        ):
            try:
                neutralize_epub(io.BytesIO(raw), cast(BinaryIO, out), name=path.name)
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
            digest = hashlib.sha256()
            with derivative.open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
            yield PreparedRange(
                item_id, str(item.edition_id), derivative, "sha256:" + digest.hexdigest(),
                len(toc.spine), report, bool(item.closed),
            )  # fmt: skip

    # -- facts ------------------------------------------------------------------------------

    def served_copy(self, destination_id: str, item_id: str) -> ServedCopy | None:
        """The file the manifest says the destination holds for the item, or None."""
        record, shelf, dest = export.export_record, export.shelf, export.shelf_destination
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(record.c.file_hash, shelf.c.skip_read_upgrades)
                .select_from(
                    record.join(dest, dest.c.id == record.c.shelf_destination_id).join(
                        shelf, shelf.c.id == dest.c.shelf_id
                    )
                )
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == item_id,
                    record.c.state == "written",
                    record.c.file_hash.is_not(None),
                )
            ).one_or_none()
        if row is None:
            return None
        return ServedCopy(str(row.file_hash), bool(row.skip_read_upgrades))

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
                "SELECT 1 FROM reading_progress p JOIN unit_coverage c ON c.unit_id = p.unit_id "
                "WHERE c.carrier_type = 'export_item' AND c.carrier_id = :i "
                "AND p.state = 'read' LIMIT 1"
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
            if served.file_hash == prepared.file_hash:
                return RangedExportOutcome("kept", "up_to_date")
            suspect = (
                suspect_reason(
                    stored_pages=served.spine_count, new_pages=prepared.spine_count,
                    availability="free", min_ratio=self._min_ratio,
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
