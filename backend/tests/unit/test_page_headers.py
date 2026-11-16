"""M4 L53 (ENF-SEC-003, ADR-0054): the size a page announces in its header, read without decoding.

A JPEG, PNG, GIF or WebP page is kept as it is, so its header is the only place a bomb can be
refused before a reader decodes it at full size."""

import io
import struct

import pytest
from PIL import Image

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust.page_header import announced_size, needs_more
from shelfhold.infrastructure.packaging.cbz import normalize_cbz
from tests import page_formats_env as pf

HUGE = 65_535  # the most a JPEG or a GIF can announce


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\0\0\0\0"


def _jpeg(width: int, height: int) -> bytes:
    soi, app = b"\xff\xd8", b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return soi + app + sof + b"\xff\xd9"


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _webp_x(width: int, height: int) -> bytes:
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    body = b"WEBPVP8X" + struct.pack("<I", 10) + b"\0\0\0\0" + canvas
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.req("ENF-SEC-003")
@pytest.mark.parametrize(
    ("kind", "make"), [("png", _png), ("jpeg", _jpeg), ("gif", _gif), ("webp", _webp_x)]
)
def test_the_announced_size_is_read_from_the_header(kind: str, make: object) -> None:
    assert announced_size(make(321, 123), kind) == (321, 123)  # type: ignore[operator]


@pytest.mark.req("ENF-SEC-003")
def test_a_jpeg_frame_header_pushed_far_in_asks_for_more_bytes() -> None:
    padding = b"\xff\xe1" + struct.pack(">H", 6000) + b"\0" * 5998
    data = _jpeg(40, 30)
    long = data[:2] + padding + data[2:]
    assert needs_more(long[:4096], "jpeg") and announced_size(long, "jpeg") == (40, 30)


@pytest.mark.req("ENF-SEC-003")
@pytest.mark.parametrize("make", [_png, _jpeg, _gif, _webp_x])
def test_a_page_in_an_allowed_format_that_announces_a_bomb_is_refused(make: object) -> None:
    archive = pf.cbz([("1.x", make(HUGE, HUGE)), ("2.jpg", pf.jpeg())])  # type: ignore[operator]
    with safe_open_archive(io.BytesIO(archive), name="in.cbz") as source:
        report = normalize_cbz(source, io.BytesIO())
    assert report.status == "kept" and [i.code for i in report.issues] == ["page.too_large"]


@pytest.mark.req("ENF-SEC-003")
def test_an_ordinary_page_is_not_refused() -> None:
    archive = pf.cbz([("1.jpg", pf.jpeg()), ("2.png", pf.png_alpha(10))])
    with safe_open_archive(io.BytesIO(archive), name="in.cbz") as source:
        assert normalize_cbz(source, io.BytesIO()).status == "unchanged"


@pytest.mark.req("EF-EXP-046")
def test_a_16_bit_tiff_with_a_gradient_keeps_its_tones() -> None:
    from shelfhold.infrastructure.imaging import reencode_image

    image = Image.new("I;16", (64, 8))
    image.putdata([x * 1000 for _ in range(8) for x in range(64)])
    out = io.BytesIO()
    image.save(out, "TIFF")
    page = Image.open(io.BytesIO(reencode_image(out.getvalue()).data)).convert("L")
    values = {page.getpixel((x, 4)) for x in range(64)}
    assert len(values) > 20 and min(values) < 20 and 200 < max(values) < 255  # not uniform
