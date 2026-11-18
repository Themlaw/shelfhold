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
- A file that cannot be hashed never fails the export: the row is made with empty hashes and
  `render_hooks.heal_alias_hashes` fills them later.
"""

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.application.render_hooks import sample_hashes
from shelfhold.infrastructure.schema import export, storage

__all__ = ["alias_wanted", "record_export_alias"]


def alias_wanted(file_hash: str, source_sha256: str | None) -> bool:
    """False only when the file is the source itself; an unknown source needs the alias."""
    return source_sha256 is None or file_hash != source_sha256


def record_export_alias(
    connection: Connection,
    *,
    destination_id: str | None,
    export_item_id: str,
    file_hash: str,
    path: Path,
) -> bool:
    """Make the alias of the file at `path` for its item, in the caller's transaction. True when a
    row was added. A row that exists keeps its place; only hashes it lacks are completed.

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
    hashes = sample_hashes(path)
    values = {
        "koreader_partial_md5": hashes.koreader_partial_md5 if hashes else None,
        "kavita_md5": hashes.kavita_md5 if hashes else None,
    }
    found = connection.execute(
        sa.select(alias.c.koreader_partial_md5, alias.c.kavita_md5).where(
            alias.c.export_item_id == export_item_id, alias.c.file_hash == file_hash
        )
    ).first()
    if found is not None:
        if hashes is not None and (found.koreader_partial_md5 is None or found.kavita_md5 is None):
            connection.execute(
                sa.update(alias)
                .where(alias.c.export_item_id == export_item_id, alias.c.file_hash == file_hash)
                .values(**values)
            )
        return False
    connection.execute(
        sa.insert(alias).values(
            export_item_id=export_item_id, file_hash=file_hash,
            content_hash=source.content_hash or file_hash,
            shelf_destination_id=destination_id, **values,
        )
    )  # fmt: skip
    return True
