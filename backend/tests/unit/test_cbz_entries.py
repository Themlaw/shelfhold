"""EF-BIB-051 (6): the CBZ entry allowlist; extension must agree with the magic bytes."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust import (
    UNTRUSTED_ENTRY_CODE,
    UntrustedEntryError,
    check_cbz_entries,
    sniff_image_kind,
)


def _check(tmp_path: Path, data: bytes) -> object:
    path = tmp_path / "issue.cbz"
    path.write_bytes(data)
    with safe_open_archive(path) as archive:
        return check_cbz_entries(archive)


def _refused(tmp_path: Path, data: bytes) -> UntrustedEntryError:
    with pytest.raises(UntrustedEntryError) as caught:
        _check(tmp_path, data)
    assert caught.value.code == "content.untrusted_entry" == UNTRUSTED_ENTRY_CODE
    return caught.value


@pytest.mark.req("EF-BIB-051")
def test_cbz_allowlist_accepts_the_synthetic_comic(malicious: ModuleType, tmp_path: Path) -> None:
    listing = _check(tmp_path, malicious.good_cbz(4))
    assert listing.images == ("001.png", "002.png", "003.png", "004.png")  # type: ignore[attr-defined]
    assert listing.comicinfo == "ComicInfo.xml"  # type: ignore[attr-defined]


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    ("name", "head"),
    [
        ("a.jpg", "JPEG_MAGIC"), ("a.jpeg", "JPEG_MAGIC"), ("a.JPG", "JPEG_MAGIC"),
        ("a.png", "PNG_MAGIC"), ("a.webp", "WEBP_MAGIC"), ("a.gif", "GIF_MAGIC"),
        ("a.avif", "AVIF_MAGIC"), ("a.jxl", "JXL_CODESTREAM_MAGIC"),
        ("a.jxl", "JXL_CONTAINER_MAGIC"),
    ],
)  # fmt: skip
def test_cbz_every_allowlisted_format_is_accepted(
    malicious: ModuleType, tmp_path: Path, name: str, head: str
) -> None:
    payload = getattr(malicious, head) + b"\x00" * 32
    listing = _check(tmp_path, malicious.cbz_with((name, payload)))
    assert listing.images == (name,)  # type: ignore[attr-defined]


@pytest.mark.req("EF-BIB-051")
def test_cbz_svg_entry_rejected_or_rasterized(malicious: ModuleType, tmp_path: Path) -> None:
    """M1a: refused with the stable code (rasterisation arrives in M2)."""
    error = _refused(tmp_path, malicious.cbz_with(("001.svg", malicious.SVG_WITH_SCRIPT)))
    assert error.entry == "001.svg"
    # an SVG hiding behind an image extension is refused too
    error = _refused(tmp_path, malicious.cbz_with(("001.png", malicious.SVG_WITH_SCRIPT)))
    assert error.entry == "001.png"


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "name",
    ["page.html", "page.xhtml", "page.htm", "run.js", "x.exe", "notes.txt", "noext", "x.png.svg"],
)
def test_cbz_entries_outside_the_allowlist_are_refused(
    malicious: ModuleType, tmp_path: Path, name: str
) -> None:
    error = _refused(tmp_path, malicious.cbz_with((name, malicious.HTML_WITH_SCRIPT)))
    assert error.entry == name


@pytest.mark.req("EF-BIB-051")
def test_cbz_extension_must_match_magic_bytes(malicious: ModuleType, tmp_path: Path) -> None:
    error = _refused(tmp_path, malicious.cbz_with(("001.jpg", malicious.HTML_WITH_SCRIPT)))
    assert error.entry == "001.jpg"
    assert error.reason == "magic_mismatch"
    # a real PNG named .jpg is also a mismatch: the extension is a promise
    assert _refused(tmp_path, malicious.cbz_with(("001.jpg", malicious.tiny_png()))).reason == (
        "magic_mismatch"
    )
    assert _refused(tmp_path, malicious.cbz_with(("001.png", b""))).reason == "magic_mismatch"
    assert (
        _refused(tmp_path, malicious.cbz_with(("001.webp", b"RIFF\x00\x00\x00\x00WAVEfmt "))).reason
        == "magic_mismatch"
    )


@pytest.mark.req("EF-BIB-051")
def test_cbz_comicinfo_is_parsed_by_the_hardened_parser(
    malicious: ModuleType, tmp_path: Path
) -> None:
    data = malicious.zip_bytes(
        [("001.png", malicious.tiny_png()), ("ComicInfo.xml", malicious.xxe_file())]
    )
    assert _refused(tmp_path, data).entry == "ComicInfo.xml"
    data = malicious.zip_bytes(
        [("001.png", malicious.tiny_png()), ("ComicInfo.xml", b"<a><b></a>")]
    )
    assert _refused(tmp_path, data).entry == "ComicInfo.xml"


@pytest.mark.req("EF-BIB-051")
def test_cbz_without_pages_or_with_two_comicinfo_is_refused(
    malicious: ModuleType, tmp_path: Path
) -> None:
    assert _refused(tmp_path, malicious.zip_bytes([("ComicInfo.xml", malicious.COMICINFO)]))
    data = malicious.zip_bytes(
        [
            ("001.png", malicious.tiny_png()),
            ("a/ComicInfo.xml", b"<a/>"),
            ("ComicInfo.xml", b"<a/>"),
        ]
    )
    assert _refused(tmp_path, data).reason == "duplicate_comicinfo"


@pytest.mark.req("EF-BIB-051")
def test_cbz_directory_entries_are_ignored(malicious: ModuleType, tmp_path: Path) -> None:
    data = malicious.zip_bytes([("pages/", b""), ("pages/001.png", malicious.tiny_png())])
    assert _check(tmp_path, data).images == ("pages/001.png",)  # type: ignore[attr-defined]


@pytest.mark.req("EF-BIB-051")
def test_sniff_image_kind_reads_only_magic_bytes() -> None:
    assert sniff_image_kind(b"\x89PNG\r\n\x1a\n....") == "png"
    assert sniff_image_kind(b"\xff\xd8\xff\xdb") == "jpeg"
    assert sniff_image_kind(b"GIF87a") == "gif"
    assert sniff_image_kind(b"<svg") is None
    assert sniff_image_kind(b"") is None
