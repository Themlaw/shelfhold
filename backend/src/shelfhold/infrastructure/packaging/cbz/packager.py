"""Build the exported CBZ: the staging file is built whole, then renamed by fsops (EF-EXP-040).

`ComicInfo.xml` is written as the LAST entry: Kavita reads it from the end of the archive, and a
file with a ComicInfo in front is rewritten rather than appended to. Any ComicInfo already in the
source is dropped; ours replaces it. Nothing identifies Shelfhold in the result (EF-EXP-063): see
`ArchiveWriter` for the container, `render_comicinfo` for the metadata.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import BinaryIO, Final

from shelfhold.infrastructure.archives import ArchiveWriter, SafeArchive
from shelfhold.infrastructure.content_trust import check_cbz_entries

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
