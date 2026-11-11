"""Receive a CBZ from a connected service: streamed, bounded, then audited (ENF-SEC-002).

The bytes go to an anonymous-named temporary file in the staging folder, chunk by chunk, counted
and hashed on the way; the bound is checked on the declared length and on what actually arrives.
Nothing here opens the archive: `PassthroughStore.ingest` does, through `safe_open_archive` and the
CBZ allowlist, before anything is stored. The file is removed when the `with` block ends.
"""

import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from shelfhold.infrastructure.fetcher import FetchError, GuardedFetcher


@dataclass(frozen=True)
class Received:
    path: Path  # the staged file; its name says nothing of what it holds (`sniff_kind` does)
    size: int
    sha256: str  # `sha256:<hex>`, of the bytes as received


EPUB_MIMETYPE = b"application/epub+zip"
EPUB_EXTENSION = ".epub"
CBZ_EXTENSION = ".cbz"
_LOCAL_HEADER = 30  # bytes of a ZIP local file header, before the name
_HEAD = 4096


def sniff_kind(path: Path) -> str:
    """`.epub` when the bytes are an EPUB container (OCF: the first entry is a stored `mimetype`
    that holds `application/epub+zip`), `.cbz` otherwise: the extension that routes a received
    file (EF-BIB-051).

    What a file is called, by the source or by its MIME type, decides nothing: an EPUB sent as a
    `.cbz` must reach the neutralizer, and a file that claims to be an EPUB without the signature
    is audited as a CBZ, whose allowlist refuses anything but images. Only the first local header
    is read (4 KiB), by hand: opening the archive is `safe_open_archive`'s job, done by the store.
    """
    try:
        with path.open("rb") as stream:
            head = stream.read(_HEAD)
    except OSError:
        return CBZ_EXTENSION
    if len(head) < _LOCAL_HEADER or head[:4] != b"PK\x03\x04":
        return CBZ_EXTENSION
    stored = int.from_bytes(head[8:10], "little") == 0
    name_length = int.from_bytes(head[26:28], "little")
    extra_length = int.from_bytes(head[28:30], "little")
    start = _LOCAL_HEADER + name_length + extra_length
    name = head[_LOCAL_HEADER : _LOCAL_HEADER + name_length]
    body = head[start : start + len(EPUB_MIMETYPE)]
    if stored and name == b"mimetype" and body == EPUB_MIMETYPE:
        return EPUB_EXTENSION
    return CBZ_EXTENSION


@asynccontextmanager
async def receive_cbz(
    fetcher: GuardedFetcher,
    url: str,
    *,
    staging_dir: Path,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    total_timeout: float = 900.0,
) -> AsyncIterator[Received]:
    with tempfile.NamedTemporaryFile(dir=staging_dir, suffix=".cbz", prefix="recv-") as sink:
        result = await fetcher.download(
            url, sink, max_bytes=max_bytes, headers=headers, total_timeout=total_timeout
        )
        if not 200 <= result.status < 300:
            raise FetchError(f"the service answered {result.status}, not a file")
        sink.flush()
        yield Received(Path(sink.name), result.size, result.sha256)
