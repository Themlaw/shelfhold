"""The image a native folder writes as the cover of a series (EF-EXP-010).

Mihon finds the file named `cover.*` and reads the image by its content; a reader that trusts the
extension (the server of Suwayomi announces `image/jpeg` for a `.jpg`) does not. So the name says
what the bytes are: `cover.jpg`, `cover.png` or `cover.webp`, by the first bytes of the page. A
first page in another format (AVIF, JXL, GIF...) is converted to WebP by the isolated image worker
(no network, limits on memory, CPU, bytes and pixels: the page is an untrusted image), the same
one that makes the covers Kavita takes. Pages inside the chapter files are never touched.
"""

from typing import Final

from shelfhold.export.domain.native_folder import cover_extension
from shelfhold.infrastructure.imaging import ImageKind, ImageRejected, make_thumbnail
from shelfhold.infrastructure.imaging.errors import WORKER_FAILED

__all__ = ["prepare_cover"]

_COVER_EDGE: Final = 1600  # longest side of a cover converted from another format


def prepare_cover(image: bytes) -> tuple[bytes, str] | None:
    """The bytes to write and their extension; None when the worker refuses the page (no cover is
    better than a wrong one). A worker that fails by itself raises: the next pass asks again."""
    extension = cover_extension(image)
    if extension is not None:
        return image, extension
    try:
        thumbnail = make_thumbnail(image, ImageKind.COVER, max_edge=_COVER_EDGE, fmt="webp")
    except ImageRejected as error:
        if error.code == WORKER_FAILED:
            raise
        return None
    return thumbnail.data, "webp"
