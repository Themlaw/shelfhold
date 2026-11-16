"""M4 L53 (EF-EXP-046 amended by ADR-0054): the pages of a CBZ are brought to a format every reader
takes. JPEG, PNG, GIF and WebP are kept, recognised by their magic bytes and never by their
extension; any other image is re-encoded by the decode worker. Name and order of the pages are
kept, only the extension changes; ComicInfo.xml and what is not an image are copied; the rewrite is
atomic for the caller (a staging file) and idempotent; a page that cannot be read is reported with a
closed code and the archive is kept as it was received."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
from PIL import Image

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust import check_cbz_entries
from shelfhold.infrastructure.content_trust.cbz_entries import sniff_page_kind
from shelfhold.infrastructure.imaging import ImageRejected, reencode_image
from shelfhold.infrastructure.imaging.client import WorkerLimits
from shelfhold.infrastructure.packaging.cbz import PageTarget, normalize_cbz
from tests import page_formats_env as env

pytestmark = pytest.mark.skipif(not env.has_avif(), reason="this Pillow has no AVIF")

LIMITS = WorkerLimits(memory_bytes=4 * 1024**3, cpu_seconds=30, wall_seconds=60)


def run(data: bytes, target: PageTarget | None = None) -> tuple[Any, bytes]:
    out = io.BytesIO()
    with safe_open_archive(io.BytesIO(data), name="in.cbz") as archive:
        report = normalize_cbz(archive, out, target=target or PageTarget())
    return report, out.getvalue()


def entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {i.filename: archive.read(i) for i in archive.infolist()}


@pytest.mark.req("EF-EXP-046")
def test_page_kinds_are_read_from_magic_bytes() -> None:
    assert sniff_page_kind(env.jpeg()[:16]) == "jpeg"
    assert sniff_page_kind(env.png_alpha()[:16]) == "png"
    assert sniff_page_kind(env.webp()[:16]) == "webp"
    assert sniff_page_kind(env.avif()[:16]) == "avif"
    assert sniff_page_kind(env.bmp()[:16]) == "bmp"
    assert sniff_page_kind(env.tiff()[:16]) == "tiff"
    assert sniff_page_kind(env.JXL_SIGNATURE[:16]) == "jxl"
    assert sniff_page_kind(env.HEIC_SIGNATURE[:16]) == "heic"
    assert sniff_page_kind(b"<html>") is None
    assert sniff_page_kind(b"") is None


@pytest.mark.req("EF-EXP-046")
def test_avif_page_becomes_jpeg_keeping_name_and_order() -> None:
    report, out = run(env.mixed_cbz())
    assert report.status == "normalized" and report.converted == 1 and not report.issues
    assert env.names(out) == ["001.jpg", "002.png", "003.jpg", "004.webp", "ComicInfo.xml"]
    pages = entries(out)
    assert sniff_page_kind(pages["001.jpg"][:16]) == "jpeg"
    original = entries(env.mixed_cbz())
    assert pages["003.jpg"] == original["003.jpg"]  # a kept page is copied byte for byte
    assert pages["004.webp"] == original["004.webp"]
    with safe_open_archive(io.BytesIO(out), name="out.cbz") as archive:
        check_cbz_entries(archive)  # nothing off the allowlist is left


@pytest.mark.req("EF-EXP-046")
def test_jpeg_target_flattens_alpha_on_white() -> None:
    _, out = run(env.cbz([("001.avif", env.avif_alpha(0)), ("002.avif", env.avif())]))
    page = Image.open(io.BytesIO(entries(out)["001.jpg"])).convert("RGB")
    r, g, b = page.getpixel((10, 10))
    assert min(r, g, b) >= 245  # fully transparent: white, not black


@pytest.mark.req("EF-EXP-046")
def test_png_alone_is_kept_even_with_alpha() -> None:
    report, out = run(env.cbz([("001.png", env.png_alpha(10)), ("002.jpg", env.jpeg())]))
    assert report.status == "unchanged" and out == b""


@pytest.mark.req("EF-EXP-046")
@pytest.mark.parametrize(("fmt", "kind", "ext"), [("png", "png", "png"), ("webp", "webp", "webp")])
def test_target_format_is_settable(fmt: str, kind: str, ext: str) -> None:
    _, out = run(env.cbz([("001.avif", env.avif())]), PageTarget(format=fmt, quality=80))
    assert env.names(out)[0] == f"001.{ext}"
    assert sniff_page_kind(entries(out)[f"001.{ext}"][:16]) == kind


@pytest.mark.req("EF-EXP-046")
def test_target_quality_changes_the_size() -> None:
    big = env.avif(size=(256, 256))
    _, low = run(env.cbz([("1.avif", big)]), PageTarget(quality=20))
    _, high = run(env.cbz([("1.avif", big)]), PageTarget(quality=95))
    assert len(entries(low)["1.jpg"]) < len(entries(high)["1.jpg"])


@pytest.mark.req("EF-EXP-046")
def test_comicinfo_is_copied_and_page_facts_follow_the_new_pages() -> None:
    _, out = run(env.mixed_cbz())
    info = entries(out)["ComicInfo.xml"].decode()
    assert "<PageCount>4</PageCount>" in info and "<Series>S</Series>" in info
    assert 'Type="Story"' in info  # what the normalizer does not own is untouched
    converted = entries(out)["001.jpg"]
    assert (
        f'<Page Image="0" ImageSize="{len(converted)}" ImageWidth="64" ImageHeight="48"/>' in info
    )
    assert 'ImageSize="3"' in info  # a page that was kept keeps the figures it had


@pytest.mark.req("EF-EXP-046")
def test_files_that_are_not_images_are_copied_as_they_are() -> None:
    data = env.cbz(
        [("001.avif", env.avif()), ("notes.txt", b"hello"), ("ComicInfo.xml", env.COMICINFO)]
    )
    _, out = run(data)
    assert entries(out)["notes.txt"] == b"hello"


@pytest.mark.req("EF-EXP-046")
def test_a_name_collision_is_resolved_and_the_order_stays() -> None:
    data = env.cbz([("001.avif", env.avif()), ("001.jpg", env.jpeg()), ("002.avif", env.avif())])
    report, out = run(data)
    assert report.status == "normalized"
    listed = env.names(out)
    assert len(set(n.lower() for n in listed)) == 3 and listed[1] == "001.jpg"
    assert listed[0].startswith("001") and listed[2].startswith("002")
    assert [o for o, _ in report.names] == ["001.avif", "001.jpg", "002.avif"]


@pytest.mark.req("EF-EXP-046")
def test_extension_is_not_trusted_a_png_named_page_holding_avif_is_converted() -> None:
    report, out = run(env.cbz([("001.png", env.avif()), ("002.avif", env.jpeg())]))
    pages = entries(out)
    assert sniff_page_kind(pages["001.jpg"][:16]) == "jpeg" and report.converted == 1
    assert pages["002.jpg"] == env.jpeg()  # a JPEG called .avif is kept, its extension is made true


@pytest.mark.req("EF-EXP-046")
def test_a_normalized_archive_is_not_rewritten() -> None:
    _, first = run(env.mixed_cbz())
    report, second = run(first)
    assert report.status == "unchanged" and second == b"" and report.converted == 0


@pytest.mark.req("EF-EXP-046")
def test_the_same_archive_gives_the_same_bytes() -> None:
    _, a = run(env.mixed_cbz())
    _, b = run(env.mixed_cbz())
    assert a == b


@pytest.mark.req("EF-EXP-046")
def test_bmp_and_tiff_are_re_encoded() -> None:
    report, out = run(env.cbz([("1.bmp", env.bmp()), ("2.tif", env.tiff())]))
    assert report.converted == 2 and env.names(out) == ["1.jpg", "2.jpg"]


@pytest.mark.req("EF-EXP-046")
def test_an_empty_page_is_reported_and_the_archive_is_kept_as_received() -> None:
    report, out = run(
        env.cbz([("001.avif", env.avif()), ("002.jpg", b""), ("003.jpg", env.jpeg())])
    )
    assert report.status == "kept"
    assert [(i.entry, i.code) for i in report.issues] == [("002.jpg", "page.empty")]
    assert out == b"" or env.names(out) == []  # nothing usable was produced


@pytest.mark.req("EF-EXP-046", "ENF-SEC-003")
def test_a_truncated_avif_is_reported_undecodable() -> None:
    report, _ = run(env.cbz([("001.avif", env.truncated_avif()), ("002.jpg", env.jpeg())]))
    assert report.status == "kept"
    assert [(i.entry, i.code) for i in report.issues] == [("001.avif", "page.undecodable")]


@pytest.mark.req("EF-EXP-046", "ENF-SEC-003")
def test_a_100000_by_100000_bmp_is_refused_before_a_pixel_is_decoded() -> None:
    report, _ = run(env.cbz([("001.bmp", env.bmp_bomb()), ("002.jpg", env.jpeg())]))
    assert report.status == "kept"
    assert [(i.entry, i.code) for i in report.issues] == [("001.bmp", "page.too_large")]


@pytest.mark.req("EF-EXP-046")
def test_formats_without_a_decoder_are_refused_with_a_closed_code() -> None:
    report, _ = run(env.cbz([("1.jxl", env.JXL_SIGNATURE), ("2.heic", env.HEIC_SIGNATURE)]))
    assert report.status == "kept"
    assert {(i.entry, i.code) for i in report.issues} == {
        ("1.jxl", "page.format_unsupported"),
        ("2.heic", "page.format_unsupported"),
    }


@pytest.mark.req("EF-EXP-046")
def test_every_problem_page_is_reported_not_only_the_first() -> None:
    report, _ = run(
        env.cbz([("1.jpg", b""), ("2.avif", env.truncated_avif()), ("3.jxl", env.JXL_SIGNATURE)])
    )
    assert len(report.issues) == 3


@pytest.mark.req("EF-EXP-046", "ENF-SEC-003")
def test_the_worker_does_the_decoding_and_applies_its_caps() -> None:
    result = reencode_image(env.avif(), fmt="jpeg", quality=90, limits=LIMITS)
    assert (result.format, result.width, result.height) == ("JPEG", 64, 48)
    with pytest.raises(ImageRejected) as caught:
        reencode_image(env.bmp_bomb(), fmt="jpeg", quality=90, limits=LIMITS)
    assert caught.value.code == "image.too_large"
    with pytest.raises(ValueError):
        reencode_image(env.avif(), fmt="avif", quality=90)  # never transcoded to AVIF or JXL
