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

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Final, cast

from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.content_trust import UntrustedEntryError, check_cbz_entries
from shelfhold.infrastructure.imaging import ImageRejected, rasterize_svg
from shelfhold.infrastructure.packaging.cbz import rasterize_cbz
from shelfhold.library.adapters.storage import KeepCopier, PassthroughStore
from shelfhold.library.domain.failures import SafeguardFailedError

SVG_DERIVED_BY: Final = "svg-rasterizer@v1"


def draw_svg(data: bytes) -> bytes:
    """The production rasterizer: the decode worker, under its default limits."""
    return rasterize_svg(data).png


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


class SvgCbzStore:
    """`PassthroughStore.ingest`, plus the derivative of an archive with SVG pages."""

    def __init__(
        self,
        store: PassthroughStore,
        keeper: KeepCopier,
        workdir: Path,
        rasterize: Callable[[bytes], bytes] = draw_svg,
    ) -> None:
        self._store = store
        self._keeper = keeper
        self._workdir = workdir
        self._rasterize = rasterize

    def ingest(self, **arguments: Any) -> Any:
        staged: Path = arguments["staged"]
        try:
            with safe_open_archive(staged, name="received.cbz") as archive:
                has_svg = bool(check_cbz_entries(archive, allow_svg=True).svgs)
        except (UnsafeArchiveError, UntrustedEntryError):
            has_svg = False  # the plain store refuses it, with its own error
        if not has_svg:
            return self._store.ingest(**arguments)
        with svg_derivative_of(staged, self._workdir, self._rasterize) as derived:
            ingested = self._store.ingest(**arguments, allow_svg_pages=True)
            if derived is None:  # pragma: no cover - has_svg said there is a page
                raise UntrustedEntryError("not_allowlisted")
            try:
                self._keeper.declare_derivative(arguments["unit_id"], derived, SVG_DERIVED_BY)
            except SafeguardFailedError as error:
                raise OSError(11, f"svg_derivative_pending: {error.reason}") from error
        return ingested
