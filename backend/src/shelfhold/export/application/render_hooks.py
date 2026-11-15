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
