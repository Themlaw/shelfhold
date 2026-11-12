"""The derivative of an untrusted CBZ that has SVG pages (EF-BIB-051 (6), M2 L10).

The archive as received is the original (`PassthroughStore.ingest(allow_svg_pages=True)`). Here
each SVG page is drawn to a lossless PNG by the decode worker (`rasterize`, bounded in memory and
time, no network, no file read), the archive is rewritten with `write_cbz`, and the result is
declared a derivative (`derived_hash`, `derived_by`): the original goes to /store byte for byte,
the rewritten file takes its place in /library, and the two count as independent copies.

One refused page refuses the whole archive: nothing is declared, the original stays where it was
(refused by every export and serve path, which allow no SVG), and the caller quarantines the Unit.
"""

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.content_trust import UntrustedEntryError
from shelfhold.infrastructure.imaging import ImageRejected
from shelfhold.infrastructure.packaging.cbz import rasterize_cbz
from shelfhold.library.adapters.storage import Derivative, KeepCopier
from shelfhold.library.domain.failures import SafeguardFailedError

SVG_DERIVED_BY: Final = "svg-rasterizer@v1"


def rasterize_svg_pages(
    keeper: KeepCopier,
    unit_id: str,
    library_file: Path,
    workdir: Path,
    rasterize: Callable[[bytes], bytes],
) -> Derivative:
    """Draw the SVG pages of `library_file`, then declare the rewritten archive a derivative.

    `workdir` holds the staging file for the time of the call. Raises `SafeguardFailedError`
    (`svg_refused`, the reason in the detail) when the archive is not one a derivative can be made
    of; the original is then untouched and nothing is declared."""
    with tempfile.TemporaryDirectory(prefix="svg-derived-", dir=workdir) as staging:
        derived = Path(staging) / "derived.cbz"
        try:
            with (
                safe_open_archive(library_file, name="original.cbz") as archive,
                derived.open("wb") as out,
            ):
                drawn = rasterize_cbz(archive, out, rasterize)
        except (ImageRejected, UntrustedEntryError, UnsafeArchiveError) as error:
            raise SafeguardFailedError("svg_refused", str(error)[:120]) from error
        if drawn == 0:
            raise SafeguardFailedError("no_svg_page", "nothing to rasterize")
        return keeper.declare_derivative(unit_id, derived, SVG_DERIVED_BY)
