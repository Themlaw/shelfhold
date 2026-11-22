"""The alias of a file an export wrote (M4 L47, EF-LEC-009, ENF-QUAL-005, ADR-0053).

An e-reader names a document by the partial MD5 of the file it holds, and the kosync routes find
the document through `document_alias`. A first export used to leave no row (only an assembly or a
rewrite did), so the digest of a freshly exported file was unknown. The row is now made in the
transaction that completes the manifest line of the export, so that a file and its alias are
committed together, or neither.

- A file whose hash is the one of the source of its document (a copy, byte for byte) gets none:
  it is the source.
- Idempotent: `(export_item_id, file_hash)` is the key, a second pass (a replay, a boot that
  completes the line, a second binding writing the same bytes) adds no row.
- The alias holds no profile, no household and no title: the household and account scoping is the
  one of the Edition, applied by the reader of the alias (`reading.application.kosync`), and the
  row follows its `export_item` (`ON DELETE CASCADE`) when the document is erased.
- The row is made **without hashes** inside the transaction (file and alias stay atomic), and the
  transaction never opens the exported path: a link, a FIFO or another file put there cannot lend
  its bytes to the alias nor hold the database write lock. After the commit, the hashes are filled
  through `render_hooks.heal_alias_hashes`: SHA-256 checked against the alias in the same single
  pass as the samples, no link followed, no blocking open, regular files only. A failure leaves
  the alias without hashes and a log line, never an exception towards the export. Two doors fill
  them, one contract: `complete_hashes`, for the one file a caller just placed (a lazy read of
  the kosync relay), and `complete_hashes_of_destination`, for the aliases of one destination
  that are still empty — the ones a plan's own operations did not get to fill, then, for what a
  stopped plan left behind, the sweep of the window and of a run with nothing to write. Both are
  bounded to the destination's aliases and read only their own records.
"""

import os
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.render_hooks import heal_alias_hashes
from shelfhold.infrastructure.logging import escaped
from shelfhold.infrastructure.schema import export, storage

__all__ = [
    "alias_wanted",
    "complete_hashes",
    "complete_hashes_of_destination",
    "record_export_alias",
]

log = structlog.get_logger(__name__)


def alias_wanted(file_hash: str, source_sha256: str | None) -> bool:
    """False only when the file is the source itself; an unknown source needs the alias."""
    return source_sha256 is None or file_hash != source_sha256


def record_export_alias(
    connection: Connection,
    *,
    destination_id: str | None,
    export_item_id: str,
    file_hash: str,
    path: Path | None = None,
) -> bool:
    """Make the alias of the file at `path` for its item, in the caller's transaction. True when a
    row was added, without hashes (`path` is accepted for the callers' symmetry and never opened
    here). A row that exists is left as it is.

    `content_hash` is the one a reader's position is anchored on: the content's, else the file's own
    (a volume has no content), the very order of `progress._current_hash` and `kosync._anchor`, so
    that no position taken before the alias existed becomes a "replaced" one."""
    alias = export.document_alias
    source = connection.execute(
        sa.select(storage.content.c.original_sha256, storage.content.c.content_hash)
        .select_from(
            export.export_item.outerjoin(
                storage.content, storage.content.c.id == export.export_item.c.content_id
            )
        )
        .where(export.export_item.c.id == export_item_id)
    ).first()
    if source is None:
        return False  # the item is gone (erased meanwhile): nothing to resolve
    if not alias_wanted(file_hash, source.original_sha256):
        return False
    found = connection.execute(
        sa.select(alias.c.file_hash).where(
            alias.c.export_item_id == export_item_id, alias.c.file_hash == file_hash
        )
    ).first()
    if found is not None:
        return False
    connection.execute(
        sa.insert(alias).values(
            export_item_id=export_item_id, file_hash=file_hash,
            content_hash=source.content_hash or file_hash,
            shelf_destination_id=destination_id,
        )
    )  # fmt: skip
    return True


def complete_hashes(engine: Engine, *, export_item_id: str, file_hash: str, path: Path) -> int:
    """After the commit of the export: the hashes of the alias of `path`, or none. The file must be
    the one `file_hash` names and reached by no link (the path as it is, its real path the same).
    Never raises; returns the number of aliases filled."""
    location = Path(os.path.abspath(path))

    def locate(item: str, digest: str) -> Path | None:
        if item != export_item_id or digest != file_hash:
            return None
        return location if os.path.realpath(location) == str(location) else None

    try:
        return heal_alias_hashes(engine, locate=locate, export_item_id=export_item_id)
    except Exception:
        log.warning("document_alias.complete_failed", export_item=export_item_id,
                    path=escaped(str(path)))  # fmt: skip
        return 0


def complete_hashes_of_destination(
    engine: Engine, destination_id: str, local: Callable[[str], str]
) -> int:
    """The aliases of one destination that are still empty, filled in one pass over them
    (L47). The same contract as `complete_hashes` for each: the file is read once, its SHA-256
    checked against its alias in the same pass as the samples, reached by no link, and the
    write stays conditional on the hashes being empty, never inside a transaction — but the
    pass is bounded: only the destination's aliases are read, only their own records are
    looked up, and no other file is opened. An alias whose file is gone, moved or not the one
    it names stays empty, for the next pass to try again. Never raises; returns the number of
    aliases filled.

    Two callers: the operation itself, right after the file is placed (an alias left empty
    until then is filled before the next operation begins), and the sweep — a run with nothing
    to write, the maintenance window — for the aliases a stopped plan left behind."""
    alias, record = export.document_alias, export.export_record
    empty = sa.or_(alias.c.koreader_partial_md5.is_(None), alias.c.kavita_md5.is_(None))
    try:
        with engine.connect() as connection:
            pending = [
                (str(row.export_item_id), str(row.file_hash))
                for row in connection.execute(
                    sa.select(alias.c.export_item_id, alias.c.file_hash).where(
                        alias.c.shelf_destination_id == destination_id, empty
                    )
                )
            ]
            if not pending:
                return 0
            paths = {
                (str(row.export_item_id), str(row.file_hash)): row.rel_path
                for row in connection.execute(
                    sa.select(record.c.export_item_id, record.c.file_hash, record.c.rel_path).where(
                        record.c.shelf_destination_id == destination_id,
                        record.c.locator_type == "path",
                        record.c.export_item_id.in_([item for item, _ in pending]),
                    )
                )
            }
    except Exception:
        log.warning("document_alias.heal_read_failed", destination=destination_id)
        return 0
    filled = 0
    for item, digest in pending:
        found = paths.get((item, digest))
        if found is None:
            continue  # its file is not on this destination's tree any more
        filled += complete_hashes(
            engine, export_item_id=item, file_hash=digest, path=Path(local(found))
        )
    return filled
