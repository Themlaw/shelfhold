"""File an imported PDF as received (EF-SRC-012, PLAN-M2 D9).

A PDF can carry JavaScript for the reader of a destination, and the neutralizer only knows EPUB:
so a PDF is stored exactly as it came, as a content of format `pdf`, and **no export reads it**
(`export_sources` asks for `cbz` or `epub` by format). It is visible in the library with its
reason, `content.untrusted_pdf`; an admin's declared purchase (`publisher`) is the case the
exporter will serve as a passthrough when a destination accepts PDF.

Nothing here parses the PDF: the bounds are the size of the file, the `%PDF-` signature at the very
first byte (so a polyglot that starts as something else is not a PDF), and no ZIP central directory
at its end (a PDF that is also an archive would be read as one by whoever opens it second).
"""

from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from shelfhold.library.adapters.storage import (
    ContentTombstones,
    Ingested,
    RootLocator,
    UnitGoneError,
    WorkHolder,
    sha256_file,
    work_of_unit,
)
from shelfhold.library.domain.content_hash import FILE_HASH_BY
from shelfhold.library.domain.failures import (
    ReceivedTooLargeError,
    TombstonedContentError,
    VariantAlreadyStoredError,
)
from shelfhold.library.replica import record_replica
from shelfhold.library.tombstones.values import ContentHash

PDF_MEDIA_TYPE: Final = "application/pdf"
PDF_SIGNATURE: Final = b"%PDF-"
DEFAULT_MAX_PDF_BYTES: Final = 256 * 1024 * 1024
UNTRUSTED_PDF: Final = "content.untrusted_pdf"

log = structlog.get_logger(__name__)


class NotAPdfError(ValueError):
    """The bytes are not a plain PDF (no signature first, or an archive in disguise)."""


_EOCD: Final = b"PK\x05\x06"  # the end of a ZIP central directory
_EOCD_WINDOW: Final = 64 * 1024 + 22  # the comment of an archive is at most 64 KiB


def check_pdf_shape(path: Path) -> None:
    with path.open("rb") as stream:
        if stream.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
            raise NotAPdfError("no PDF signature at the first byte")
        size = stream.seek(0, 2)
        stream.seek(max(0, size - _EOCD_WINDOW))
        if _EOCD in stream.read():
            raise NotAPdfError("a PDF that is also an archive")


class PdfStore:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        locate: RootLocator,
        *,
        tombstones: ContentTombstones,
        hold_work: WorkHolder,
        max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._tombstones = tombstones
        self._hold_work = hold_work
        self._max = max_bytes

    def ingest(
        self,
        *,
        unit_id: str,
        variant_id: str,
        staged: Path,
        dest: str,
        expected_sha256: str | None = None,
        upstream_ref: str | None = None,
    ) -> Ingested:
        """File `staged` at `dest`; raises, having changed nothing, for a file too large, not a
        PDF, not what was announced, tombstoned, already there or for a Unit that is gone."""
        size = staged.stat().st_size
        if size > self._max:
            raise ReceivedTooLargeError(f"{size} bytes, above {self._max}")
        check_pdf_shape(staged)
        original = sha256_file(staged)
        if expected_sha256 is not None and expected_sha256 != original:
            raise ValueError("received_hash_mismatch: the file is not what the download counted")
        with self._engine.connect() as connection:
            work_id = work_of_unit(connection, unit_id)
        if work_id is None:
            raise UnitGoneError(unit_id)
        with self._hold_work(work_id):
            return self._file(unit_id, variant_id, staged, dest, original, upstream_ref)

    def _file(
        self, unit_id: str, variant_id: str, staged: Path, dest: str, original: str,
        upstream_ref: str | None,
    ) -> Ingested:  # fmt: skip
        if self._tombstones.check_content([ContentHash(FILE_HASH_BY, original)]):
            raise TombstonedContentError("the content was purged: it is not filed again")
        with self._engine.connect() as connection:
            taken = connection.execute(
                sa.select(storage.content.c.id).where(
                    storage.content.c.variant_id == variant_id,
                    storage.content.c.media_type == PDF_MEDIA_TYPE,
                )
            ).first()
        if taken is not None:
            raise VariantAlreadyStoredError(f"the Variant {variant_id} already has its PDF")
        root_id, rel = self._locate(dest)
        with staged.open("rb") as stream:
            identity = self._fsops.write_atomic(
                dest, stream, parents=True, replace=False,
                verify_digest=lambda: original.removeprefix("sha256:"),
            )  # fmt: skip
        try:
            content_id, now = new_ulid(), datetime.now(UTC)
            with self._engine.begin() as connection:
                connection.execute(
                    sa.insert(storage.content).values(
                        id=content_id, unit_id=unit_id, variant_id=variant_id, kind="file",
                        media_type=PDF_MEDIA_TYPE, format="pdf", retention="passthrough",
                        original_sha256=original, store_provenance="received",
                        size=identity.size,
                    )
                )  # fmt: skip
                replica_id = record_replica(
                    connection, unit_id=unit_id, location="library", content_hash=None,
                    file_hash=original, independent=True, mutable_by_third_party=True,
                    physical_root_id=root_id, rel_path=rel, dev=self._fsops.device_of(dest),
                    ino=identity.ino, size=identity.size, mtime_ns=identity.mtime_ns,
                    verified_at=now,
                )  # fmt: skip
                if upstream_ref is not None:
                    record_replica(
                        connection, unit_id=unit_id, location="upstream", content_hash=None,
                        file_hash=original, independent=True, mutable_by_third_party=True,
                        remote_ref=upstream_ref, size=identity.size, verified_at=now,
                    )  # fmt: skip
        except BaseException:
            with suppress(FileNotFoundError, FsopsError):
                self._fsops.purge_unlink(dest)  # the rows did not take it: no readable orphan
            raise
        log.info("pdf.stored", unit_id=unit_id, name=PurePath(dest).suffix)
        return Ingested(content_id, replica_id, original, original, identity.size, 0)
