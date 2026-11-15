"""What the OPDS route sees of the block provider (`interfaces/http_api/opds/bundle.py`)."""

from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_MEDIA_TYPE = "application/vnd.comicbook+zip"


@dataclass(frozen=True)
class BundleOffer:
    """What the feed says of a block, without opening a file: the name the reader will get, the
    exact size when a version is already made (None before: no size is promised), and the number
    of chapters left out. The real name is the one of `Content-Disposition`."""

    filename: str
    size: int | None = None
    media_type: str = DEFAULT_MEDIA_TYPE
    notice: str | None = None
    part: int = 1
    excluded: int = 0


@dataclass(frozen=True)
class BundleDownload:
    """A block ready to stream: every file already opened and checked, `size` exact. `chunks` is
    an iterator with a `close()` that gives the slot back."""

    filename: str
    size: int
    chunks: Iterable[bytes]
    media_type: str = DEFAULT_MEDIA_TYPE
    version_id: str | None = None
    content_fp: str | None = None
