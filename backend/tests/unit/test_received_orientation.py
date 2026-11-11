"""EF-BIB-051 (M2 L19): what a received file is decides where it is filed, never its name.

The follow cycle used to file every received file as a `.cbz`; a source can now hand over an EPUB,
so the extension comes from the bytes. An EPUB takes the trust sort and the neutralizer of M2 L03;
a file that only claims to be one meets the CBZ allowlist.
"""

from pathlib import Path

import pytest

from shelfhold.acquisition.public import oriented_name
from shelfhold.composition import WriteContext
from shelfhold.library.public import sniff_kind
from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, epub_bytes, xhtml, zip_bytes
from tests.library_env import cbz_bytes

EPUB = epub_bytes(xhtml("<p>x</p>"))


def staged(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "received"
    path.write_bytes(data)
    return path


@pytest.mark.req("EF-BIB-051")
def test_an_epub_is_oriented_by_its_bytes_whatever_it_is_called(tmp_path: Path) -> None:
    for name in ("book.cbz", "book.pdf", "book", "BOOK.EPUB"):
        assert oriented_name(name, staged(tmp_path, EPUB)).endswith(".epub")
    assert sniff_kind(staged(tmp_path, EPUB)) == ".epub"


@pytest.mark.req("EF-BIB-051")
def test_a_cbz_stays_a_cbz_even_when_it_is_called_an_epub(tmp_path: Path) -> None:
    assert oriented_name("fake.epub", staged(tmp_path, cbz_bytes())) == "fake.cbz"


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"PK\x03\x04",  # a header cut short
        b"%PDF-1.7 not a zip",
        # the mimetype is not the first entry
        zip_bytes(
            [
                Member("a.xhtml", b"x", deflate=False),
                Member("mimetype", b"application/epub+zip", deflate=False),
            ]
        ),
        # the mimetype is deflated: no EPUB reader accepts it, so it is no EPUB here
        zip_bytes([Member("mimetype", b"application/epub+zip", deflate=True)]),
        # the first entry holds another type
        zip_bytes([Member("mimetype", b"application/zip", deflate=False)]),
        # a name that only starts like it
        zip_bytes([Member("mimetype2", b"application/epub+zip", deflate=False)]),
    ],
)
def test_what_is_not_an_epub_container_is_a_cbz_for_the_audit(tmp_path: Path, data: bytes) -> None:
    assert sniff_kind(staged(tmp_path, data)) == ".cbz"


@pytest.mark.req("EF-BIB-051")
def test_a_missing_file_is_a_cbz_for_the_audit(tmp_path: Path) -> None:
    assert sniff_kind(tmp_path / "nothing") == ".cbz"


@pytest.mark.req("EF-BIB-051")
def test_the_inbox_path_takes_an_epub_or_a_cbz_and_nothing_else() -> None:
    context = WriteContext.__new__(WriteContext)
    context.loaded = type("L", (), {"expected": [type("E", (), {"path": "/library"})()]})()  # type: ignore[assignment]
    assert context.received_path("V1").endswith("/V1.cbz")
    assert context.received_path("V1", ".epub").endswith("/V1.epub")
    for bad in (".pdf", ".exe", "", "/../x"):
        with pytest.raises(ValueError, match="no received file"):
            context.received_path("V1", bad)
