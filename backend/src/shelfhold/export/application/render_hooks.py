"""What is done around a rendered file once it is in place (ENF-QUAL-005, EF-LEC-009).

At every rewrite of a rendered file the alias of the file gets the hashes a reader identifies it
by: KOReader's partial MD5 and Kavita's variant (`reading.domain.document_hash`), for the file that
was replaced and for the one that replaces it, so that a position taken on the old file still
resolves. The alias row itself is made in the transaction that closes the rewrite; this hook only
completes it, after that transaction, and **never** fails or undoes the export it follows: a hash
that cannot be computed or a row that cannot be written is logged, and the next pass heals it.

Hashes are stored like every digest of the schema, `md5:<lowercase hex>`, Kavita's included: its
uppercase form is the one it prints, not one worth storing, and comparing is case-blind.
"""

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.logging import escaped
from shelfhold.infrastructure.schema import export
from shelfhold.reading.domain.document_hash import kavita_hash, koreader_partial_md5

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DocumentHashes:
    koreader_partial_md5: str  # `md5:<lowercase hex>`
    kavita_md5: str  # `md5:<lowercase hex>`


def sample_hashes(path: Path) -> DocumentHashes | None:
    """The two hashes of the file at `path`, or None if they cannot be had. Never raises."""
    try:
        return DocumentHashes(
            "md5:" + koreader_partial_md5(path), "md5:" + kavita_hash(path).lower()
        )
    except Exception:
        log.warning("document_alias.hash_failed", path=escaped(str(path)))
        return None


def record_rewrite(
    engine: Engine,
    *,
    destination_id: str,
    export_item_id: str,
    path: Path,
    old_file_hash: str,
    old_hashes: DocumentHashes | None,
    new_file_hash: str,
) -> None:
    """Complete the alias rows of a rewrite committed at `path`: the old file's (hashed before it
    was replaced) and the new one's (hashed in place). Never raises."""
    try:
        written = {old_file_hash: old_hashes, new_file_hash: sample_hashes(path)}
        alias = export.document_alias
        with write_transaction(engine) as connection:
            for file_hash, hashes in written.items():
                if hashes is None:
                    continue
                connection.execute(
                    sa.update(alias)
                    .where(alias.c.export_item_id == export_item_id, alias.c.file_hash == file_hash)
                    .values(
                        koreader_partial_md5=hashes.koreader_partial_md5,
                        kavita_md5=hashes.kavita_md5,
                        shelf_destination_id=destination_id,
                    )
                )
    except Exception:
        log.warning("document_alias.write_failed", destination=destination_id)


# (item, file hash) -> (size, mtime_ns) of the file that was found NOT to be the one the alias
# names: not read again until the file changes. Bounded; a full table starts again empty.
_NOT_THE_FILE: dict[tuple[str, str], tuple[int, int]] = {}
_NOT_THE_FILE_MAX = 10_000


def _hash_once(path: Path, file_hash: str) -> DocumentHashes | bool | None:
    """SHA-256 and the sample hashes from ONE open file: False when the file is not the one named
    (its size and date are then remembered by the caller), None when it cannot be hashed. The
    bytes that were verified are the bytes that are hashed, and a file that changed while it was
    read is refused (size and date seen before and after)."""
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
        if "sha256:" + digest.hexdigest() != file_hash:
            return False
        stream.seek(0)
        koreader = "md5:" + koreader_partial_md5(stream)
        stream.seek(0)
        kavita = "md5:" + kavita_hash(stream).lower()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return DocumentHashes(koreader, kavita)


def heal_alias_hashes(
    engine: Engine,
    *,
    locate: Callable[[str, str], Path | None],
    export_item_id: str | None = None,
) -> int:
    """Fill the aliases a crash left without hashes (the hook above runs after the commit).

    For each alias with an empty KOReader or Kavita hash, `locate(export_item_id, file_hash)` says
    where the file is, or None when it is gone (a replaced file is often not kept: its hashes stay
    empty rather than invented). **Contract of `locate`** (the caller's, L47): a path **under the
    physical roots** of the item's binding, never a symbolic link (resolve it and refuse one that
    leaves the roots; the sweep opens what it is given), and None for anything else.

    The file is opened **once**: SHA-256 and the sample hashes come from the same descriptor, so
    the bytes that were verified are the bytes that are hashed, and the file must be the one the
    alias names (a file rewritten since never lends its hashes to another's alias). The write is
    conditional on the hashes still being empty. A file found not to match is not read again until
    its size or date changes. A locator or a file that fails skips that alias, never the sweep.
    With `export_item_id` only that item is healed (a lazy read by the kosync relay); without it,
    all of them (the sweep). Returns the number of aliases filled; never raises.
    """
    alias = export.document_alias
    empty = sa.or_(alias.c.koreader_partial_md5.is_(None), alias.c.kavita_md5.is_(None))
    query = sa.select(alias.c.export_item_id, alias.c.file_hash).where(empty)
    if export_item_id is not None:
        query = query.where(alias.c.export_item_id == export_item_id)
    try:
        with engine.connect() as connection:
            pending = [(str(r.export_item_id), str(r.file_hash)) for r in connection.execute(query)]
    except Exception:
        log.warning("document_alias.heal_read_failed")
        return 0
    filled = 0
    for item_id, file_hash in pending:
        try:
            path = locate(item_id, file_hash)
            if path is None:
                continue
            seen = path.stat()
            if _NOT_THE_FILE.get((item_id, file_hash)) == (seen.st_size, seen.st_mtime_ns):
                continue
            hashes = _hash_once(path, file_hash)
            if hashes is False:
                if len(_NOT_THE_FILE) >= _NOT_THE_FILE_MAX:
                    _NOT_THE_FILE.clear()
                _NOT_THE_FILE[(item_id, file_hash)] = (seen.st_size, seen.st_mtime_ns)
                continue
            if not isinstance(hashes, DocumentHashes):
                continue
            with write_transaction(engine) as connection:
                done = connection.execute(
                    sa.update(alias)
                    .where(alias.c.export_item_id == item_id, alias.c.file_hash == file_hash, empty)
                    .values(
                        koreader_partial_md5=hashes.koreader_partial_md5,
                        kavita_md5=hashes.kavita_md5,
                    )
                )
            filled += done.rowcount
        except Exception:
            log.warning("document_alias.heal_failed", export_item=item_id)
    return filled
