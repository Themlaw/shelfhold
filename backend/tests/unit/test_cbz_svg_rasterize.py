"""EF-BIB-051 (6), rasterisation volet: the CBZ rewrite that replaces SVG pages by PNG pages."""

from __future__ import annotations

import io
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust import UntrustedEntryError, check_cbz_entries
from shelfhold.infrastructure.imaging import ImageRejected
from shelfhold.infrastructure.packaging.cbz import rasterize_cbz

PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 24
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'
SCRIPTED = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def archive_of(malicious: ModuleType, tmp_path: Path, *members: tuple[str, bytes]) -> Path:
    path = tmp_path / "in.cbz"
    path.write_bytes(malicious.cbz_with(*members))
    return path


def fake_png(_svg: bytes) -> bytes:
    return PNG_A


@pytest.mark.req("EF-BIB-051")
def test_cbz_svg_entry_rejected_or_rasterized_by_default_and_when_allowed(
    malicious: ModuleType, tmp_path: Path
) -> None:
    path = archive_of(malicious, tmp_path, ("001.png", PNG_A), ("002.svg", SVG))
    with safe_open_archive(path) as archive:
        with pytest.raises(UntrustedEntryError):  # the default stays closed
            check_cbz_entries(archive)
        listing = check_cbz_entries(archive, allow_svg=True)
    assert listing.svgs == ("002.svg",) and listing.images == ("001.png",)
    assert listing.pages == ("001.png", "002.svg")


@pytest.mark.req("EF-BIB-051")
@pytest.mark.parametrize(
    "name",
    ["page.html", "page.xhtml", "run.js", "x.svgz", "x.png.svg.exe", "noext"],
)
def test_html_xhtml_script_entries_stay_refused_even_with_svg_allowed(
    malicious: ModuleType, tmp_path: Path, name: str
) -> None:
    path = archive_of(malicious, tmp_path, ("001.png", PNG_A), (name, malicious.HTML_WITH_SCRIPT))
    with safe_open_archive(path) as archive, pytest.raises(UntrustedEntryError):
        check_cbz_entries(archive, allow_svg=True)


@pytest.mark.req("EF-BIB-051")
def test_html_hiding_behind_an_svg_extension_stays_refused(
    malicious: ModuleType, tmp_path: Path
) -> None:
    path = archive_of(malicious, tmp_path, ("001.png", PNG_A), ("002.svg", malicious.HTML_WITH_SCRIPT))
    with safe_open_archive(path) as archive, pytest.raises(UntrustedEntryError) as caught:
        check_cbz_entries(archive, allow_svg=True)
    assert caught.value.kind == "magic_mismatch"


@pytest.mark.req("EF-BIB-051")
def test_rasterize_cbz_replaces_svg_pages_in_order_and_keeps_the_rest(
    malicious: ModuleType, tmp_path: Path
) -> None:
    path = archive_of(
        malicious, tmp_path, ("001.png", PNG_A), ("002.svg", SVG), ("003.jpg", malicious.JPEG_MAGIC + b"x" * 20)
    )
    out = io.BytesIO()
    with safe_open_archive(path) as archive:
        count = rasterize_cbz(archive, out, fake_png)
    assert count == 1
    out.seek(0)
    with safe_open_archive(out, name="d.cbz") as derived:
        listing = check_cbz_entries(derived)  # the derivative passes the default allowlist
        assert listing.images == ("001.png", "002.png", "003.jpg")
        assert derived.read("002.png", max_bytes=1000) == PNG_A
        assert derived.read("001.png", max_bytes=1000) == PNG_A
        assert [e.name for e in derived.entries][-1] == "ComicInfo.xml"  # ComicInfo last, as write_cbz


@pytest.mark.req("EF-BIB-051")
def test_rasterize_cbz_refuses_a_name_collision(malicious: ModuleType, tmp_path: Path) -> None:
    path = archive_of(malicious, tmp_path, ("001.png", PNG_A), ("001.svg", SVG))
    with safe_open_archive(path) as archive, pytest.raises(UntrustedEntryError) as caught:
        rasterize_cbz(archive, io.BytesIO(), fake_png)
    assert caught.value.kind == "not_allowlisted"


@pytest.mark.req("EF-BIB-051")
def test_one_refused_svg_refuses_the_whole_archive_and_writes_nothing_usable(
    malicious: ModuleType, tmp_path: Path
) -> None:
    path = archive_of(malicious, tmp_path, ("001.png", PNG_A), ("002.svg", SCRIPTED))

    def refuse(_data: bytes) -> bytes:
        raise ImageRejected("image.format_refused", "svg_script", "header")

    with safe_open_archive(path) as archive, pytest.raises(ImageRejected):
        rasterize_cbz(archive, io.BytesIO(), refuse)


@pytest.mark.req("EF-BIB-051")
def test_a_rasterizer_answering_a_non_png_is_refused(malicious: ModuleType, tmp_path: Path) -> None:
    path = archive_of(malicious, tmp_path, ("001.png", PNG_A), ("002.svg", SVG))
    with safe_open_archive(path) as archive, pytest.raises(ImageRejected):
        rasterize_cbz(archive, io.BytesIO(), lambda _d: b"<svg/>")


@pytest.mark.req("EF-BIB-051")
def test_rasterize_cbz_with_no_svg_is_a_no_op_error(malicious: ModuleType, tmp_path: Path) -> None:
    path = archive_of(malicious, tmp_path, ("001.png", PNG_A))
    with safe_open_archive(path) as archive:
        assert rasterize_cbz(archive, io.BytesIO(), fake_png) == 0


@pytest.mark.req("EF-BIB-051")
def test_real_worker_rasterizes_a_cbz_end_to_end(malicious: ModuleType, tmp_path: Path) -> None:
    from shelfhold.infrastructure.imaging import rasterize_svg

    page = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8">'
        b'<rect width="12" height="8" fill="#0a0"/></svg>'
    )
    path = archive_of(malicious, tmp_path, ("001.svg", page))
    out = io.BytesIO()
    with safe_open_archive(path) as archive:
        rasterize_cbz(archive, out, lambda data: rasterize_svg(data).png)
    out.seek(0)
    with safe_open_archive(out, name="d.cbz") as derived:
        assert check_cbz_entries(derived).images == ("001.png",)
