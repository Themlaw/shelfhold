"""Build the exported CBZ: the staging file is built whole, then renamed by fsops (EF-EXP-040).

`ComicInfo.xml` is written as the LAST entry: Kavita reads it from the end of the archive, and a
file with a ComicInfo in front is rewritten rather than appended to. Any ComicInfo already in the
source is dropped; ours replaces it. Nothing identifies Shelfhold in the result (EF-EXP-063): see
`ArchiveWriter` for the container, `render_comicinfo` for the metadata.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import BinaryIO, Final

from shelfhold.infrastructure.archives import ArchiveWriter, SafeArchive
from shelfhold.infrastructure.content_trust import UntrustedEntryError, check_cbz_entries
from shelfhold.infrastructure.imaging.errors import WORKER_FAILED, ImageRejected

SVG_MAX_BYTES: Final = 8 * 1024 * 1024
_PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
_EMPTY_COMICINFO: Final = b'<?xml version="1.0"?><ComicInfo/>'

COMICINFO_NAME: Final = "ComicInfo.xml"


def write_cbz(pages: Iterable[tuple[str, bytes]], comicinfo: bytes, out: BinaryIO) -> None:
    """Pages already validated by the caller, in reading order, then the ComicInfo."""
    with ArchiveWriter(out) as writer:
        for name, data in pages:
            if name.rsplit("/", 1)[-1].lower() == COMICINFO_NAME.lower():
                raise ValueError("a page cannot be named ComicInfo.xml")
            writer.add(name, data)
        writer.add(COMICINFO_NAME, comicinfo, compress=True)


def repackage_cbz(source: SafeArchive, comicinfo: bytes, out: BinaryIO) -> None:
    """Copy the pages of an audited source archive, in its order, and end with `comicinfo`.

    The allowlist (images by magic bytes, one ComicInfo) is applied first: an archive off the list
    is refused before a single byte is written.
    """
    listing = check_cbz_entries(source)
    with ArchiveWriter(out) as writer:
        for name in listing.images:
            with source.open(name) as page:
                writer.add(name, page)
        writer.add(COMICINFO_NAME, comicinfo, compress=True)


def rasterize_cbz(source: SafeArchive, out: BinaryIO, rasterize: Callable[[bytes], bytes]) -> int:
    """Rewrite an untrusted archive whose pages include SVG (EF-BIB-051 (6), M2 L10): each `.svg`
    page becomes a lossless PNG of the same stem, at the same place; every other page is copied
    as it is, ComicInfo.xml last. Returns the number of pages drawn (0: nothing was SVG).

    `rasterize` is the decode worker's `rasterize_svg(...).png`: it runs under its own limits and
    raises `ImageRejected` for a page it refuses, which refuses the whole archive: a derivative is
    never a book with a missing page. The result passes `check_cbz_entries` with no opt-in. The
    caller writes to a staging file and discards it on any error; the original is never touched.
    """
    listing = check_cbz_entries(source, allow_svg=True)
    if not listing.svgs:
        return 0
    taken = {name.lower() for name in listing.pages}
    renamed = {name: name.rsplit(".", 1)[0] + ".png" for name in listing.svgs}
    for new in renamed.values():
        if new.lower() in taken:
            raise UntrustedEntryError("not_allowlisted", new)
    comicinfo = (
        bytes(source.read(listing.comicinfo, max_bytes=1024 * 1024))
        if listing.comicinfo
        else _EMPTY_COMICINFO
    )

    def pages() -> Iterator[tuple[str, bytes]]:
        for name in listing.pages:
            if name not in renamed:
                yield name, bytes(source.read(name))
                continue
            png = rasterize(bytes(source.read(name, max_bytes=SVG_MAX_BYTES)))
            if not png.startswith(_PNG_MAGIC):
                raise ImageRejected(WORKER_FAILED, "not a PNG", "worker")
            yield renamed[name], png

    write_cbz(pages(), comicinfo, out)
    return len(renamed)
