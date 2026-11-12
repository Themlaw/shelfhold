"""The derivative of an untrusted CBZ that has SVG pages (EF-BIB-051 (6), M2 L10).

The archive as received is the original (`PassthroughStore.ingest(allow_svg_pages=True)`). Each SVG
page is drawn to a lossless PNG by the decode worker (`rasterize`, bounded in memory and time, no
network, no file read), the archive is rewritten with `write_cbz`, and the result is declared a
derivative (`derived_hash`, `derived_by`): the original goes to /store byte for byte, the rewritten
file takes its place in /library, and the two count as independent copies.

One refused page refuses the whole archive. `SvgCbzStore` does the drawing **before** anything is
stored, so a hostile SVG leaves no Unit, no file and no row: the caller's refusal (`ARCHIVE`) keeps
the received file where it was. It is the store the import folder, the sources and Suwayomi file
CBZ through.
"""

import errno
import hashlib
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import Any, BinaryIO, Final, cast

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.content_trust import UntrustedEntryError, check_cbz_entries
from shelfhold.infrastructure.imaging import ImageRejected, rasterize_svg
from shelfhold.infrastructure.packaging.cbz import rasterize_cbz
from shelfhold.infrastructure.schema import storage
from shelfhold.library.adapters.storage import (
    KeepCopier,
    PassthroughStore,
    WorkHolder,
    sha256_file,
    work_of_unit,
)
from shelfhold.library.domain.failures import SafeguardFailedError, VariantAlreadyStoredError

SVG_DERIVED_BY: Final = "svg-rasterizer@v1"


@contextmanager
def svg_derivative_of(
    source: Path, workdir: Path, rasterize: Callable[[bytes], bytes]
) -> Iterator[Path | None]:
    """The rewritten archive (a file that exists for the `with` block), or None when `source` has
    no SVG page. A page the worker refuses raises `UntrustedEntryError`: nothing was written to
    the destination, the staging file is gone, the original is untouched."""
    with tempfile.NamedTemporaryFile(prefix="svg-derived-", suffix=".cbz", dir=workdir) as staging:
        try:
            with safe_open_archive(source, name="original.cbz") as archive:
                drawn = rasterize_cbz(archive, cast(BinaryIO, staging), rasterize)
            staging.flush()
        except (ImageRejected, UnsafeArchiveError) as error:
            raise UntrustedEntryError("not_allowlisted") from error
        yield Path(staging.name) if drawn else None


class _DrawCache:
    """The pages drawn by the pre-check are not drawn again by the store: a small cache by the
    hash of the SVG bytes, bounded in total size."""

    def __init__(self, limit: int = 64 * 1024 * 1024) -> None:
        self._limit = limit
        self._items: dict[bytes, bytes] = {}
        self._size = 0

    def __call__(self, data: bytes) -> bytes:
        key = hashlib.sha256(data).digest()
        if key in self._items:
            return self._items[key]
        png = rasterize_svg(data).png
        while self._items and self._size + len(png) > self._limit:
            self._size -= len(self._items.pop(next(iter(self._items))))
        if len(png) <= self._limit:
            self._items[key] = png
            self._size += len(png)
        return png


draw_svg = _DrawCache()  # the production rasterizer: the decode worker, under its default limits


class SvgCbzStore:
    """`PassthroughStore.ingest`, plus the derivative of an archive with SVG pages.

    The ingestion of the original and the declaration of the derivative run under **one** hold of
    the Work (the inner stores are built with no hold of their own), so a purge never sees the one
    without the other. `finish_derivative` completes a Unit whose derivative is missing (a failed
    declaration, a replay): the import folder calls it before it removes a source."""

    def __init__(
        self,
        store: PassthroughStore,
        keeper: KeepCopier,
        workdir: Path,
        engine: Engine,
        hold_work: WorkHolder,
        rasterize: Callable[[bytes], bytes] = draw_svg,
    ) -> None:
        self._store = store
        self._keeper = keeper
        self._workdir = workdir
        self._engine = engine
        self._hold_work = hold_work
        self._rasterize = rasterize

    def _held(self, unit_id: str) -> AbstractContextManager[Any]:
        with self._engine.connect() as connection:
            work_id = work_of_unit(connection, unit_id)
        return nullcontext() if work_id is None else self._hold_work(work_id)

    @staticmethod
    def _has_svg(staged: Path) -> bool:
        try:
            with safe_open_archive(staged, name="received.cbz") as archive:
                return bool(check_cbz_entries(archive, allow_svg=True).svgs)
        except (UnsafeArchiveError, UntrustedEntryError):
            return False  # the plain store refuses it, with its own error

    def ingest(self, **arguments: Any) -> Any:
        staged: Path = arguments["staged"]
        unit_id: str = arguments["unit_id"]
        try:
            with self._held(unit_id):
                if not self._has_svg(staged):
                    return self._store.ingest(**arguments)
                with svg_derivative_of(staged, self._workdir, self._rasterize) as derived:
                    try:
                        ingested = self._store.ingest(**arguments, allow_svg_pages=True)
                    except VariantAlreadyStoredError:  # a replay: finish what the first pass left
                        if self.derivative_missing(unit_id, staged):
                            self._declare(unit_id, derived)
                        raise
                    self._declare(unit_id, derived)
                return ingested
        except SafeguardFailedError as error:  # the original is filed; the replay finishes it
            raise OSError(errno.EAGAIN, f"svg_derivative_pending: {error.reason}") from error

    def _declare(self, unit_id: str, derived: Path | None) -> None:
        if derived is None:  # pragma: no cover - the caller saw an SVG page
            raise UntrustedEntryError("not_allowlisted")
        self._keeper.declare_derivative(unit_id, derived, SVG_DERIVED_BY)

    def derivative_missing(self, unit_id: str, staged: Path) -> bool:
        """Does `staged` have SVG pages while its Unit's content has no derivative yet?"""
        if not self._has_svg(staged):
            return False
        with self._engine.connect() as connection:
            hashes = (
                connection.execute(
                    sa.select(storage.content.c.derived_hash).where(
                        storage.content.c.unit_id == unit_id,
                        storage.content.c.original_sha256 == sha256_file(staged),
                    )
                )
                .scalars()
                .all()
            )
        return not hashes or any(h is None for h in hashes)

    def finish_derivative(self, *, unit_id: str, staged: Path) -> None:
        """Declare the derivative the Unit still lacks. Raises `OSError` (EAGAIN) while it cannot,
        `UntrustedEntryError` when a page is refused; a Unit that has it is left alone."""
        try:
            with self._held(unit_id):
                if not self.derivative_missing(unit_id, staged):
                    return
                with svg_derivative_of(staged, self._workdir, self._rasterize) as derived:
                    self._declare(unit_id, derived)
        except SafeguardFailedError as error:
            raise OSError(errno.EAGAIN, f"svg_derivative_pending: {error.reason}") from error
