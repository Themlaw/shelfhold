"""M4 L53: archives of comic pages in many formats, built by the test (no binary is committed).

Pillow draws the real pages (AVIF, PNG with alpha, JPEG, WebP, BMP, TIFF). The hostile ones are
written by hand: a BMP whose header announces 100 000 x 100 000 pixels, an AVIF cut in two, a
page named `.png` that holds an AVIF, a JPEG XL signature with nothing behind it.
"""

from __future__ import annotations

import io
import struct
import zipfile
from typing import Any, Final

from PIL import Image, features

COMICINFO: Final = (
    b'<?xml version="1.0"?><ComicInfo><Series>S</Series><PageCount>4</PageCount><Pages>'
    b'<Page Image="0" ImageSize="1" ImageWidth="64" ImageHeight="48"/>'
    b'<Page Image="1" ImageSize="2" ImageWidth="64" ImageHeight="48" Type="Story"/>'
    b'<Page Image="2" ImageSize="3" ImageWidth="64" ImageHeight="48"/>'
    b'<Page Image="3" ImageSize="4" ImageWidth="64" ImageHeight="48"/>'
    b"</Pages></ComicInfo>"
)
JXL_SIGNATURE: Final = b"\x00\x00\x00\x0cJXL \r\n\x87\n" + b"\x00" * 40
HEIC_SIGNATURE: Final = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1" + b"\x00" * 40


def has_avif() -> bool:
    return bool(features.check("avif"))


def _save(image: Image.Image, fmt: str, **options: object) -> bytes:
    out = io.BytesIO()
    image.save(out, fmt, **options)
    return out.getvalue()


def avif(colour: tuple[int, int, int] = (200, 10, 10), size: tuple[int, int] = (64, 48)) -> bytes:
    return _save(Image.new("RGB", size, colour), "AVIF")


def jpeg(colour: tuple[int, int, int] = (10, 200, 10), size: tuple[int, int] = (64, 48)) -> bytes:
    return _save(Image.new("RGB", size, colour), "JPEG", quality=85)


def webp(colour: tuple[int, int, int] = (10, 10, 200)) -> bytes:
    return _save(Image.new("RGB", (64, 48), colour), "WEBP")


def png_alpha(alpha: int = 0) -> bytes:
    """A fully transparent (alpha 0) or half-transparent RGBA page: flattened on white to JPEG."""
    return _save(Image.new("RGBA", (64, 48), (255, 0, 0, alpha)), "PNG")


def avif_alpha(alpha: int = 0) -> bytes:
    """A transparent AVIF page: flattened on white when it becomes a JPEG."""
    return _save(Image.new("RGBA", (64, 48), (255, 0, 0, alpha)), "AVIF")


def bmp() -> bytes:
    return _save(Image.new("RGB", (64, 48), (50, 60, 70)), "BMP")


def tiff() -> bytes:
    return _save(Image.new("RGB", (64, 48), (50, 60, 70)), "TIFF")


def bmp_bomb(width: int = 100_000, height: int = 100_000) -> bytes:
    """A BMP header that announces `width` x `height` 24-bit pixels and carries none."""
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, 0, 2835, 2835, 0, 0)
    return b"BM" + struct.pack("<IHHI", 54, 0, 0, 54) + info


def truncated_avif() -> bytes:
    whole = avif()
    return whole[: len(whole) // 2]


def cbz(entries: list[tuple[str, bytes]]) -> bytes:
    """A stored CBZ whose bytes depend only on its arguments."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    return buffer.getvalue()


def mixed_cbz() -> bytes:
    """AVIF, PNG with alpha, JPEG and WebP pages, then a ComicInfo."""
    return cbz(
        [
            ("001.avif", avif()),
            ("002.png", png_alpha()),
            ("003.jpg", jpeg()),
            ("004.webp", webp()),
            ("ComicInfo.xml", COMICINFO),
        ]
    )


def names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [i.filename for i in archive.infolist()]


def build_normalizer(
    engine: Any,
    fsops: Any,
    locate: Any,
    *,
    store: str,
    data: Any,
    workdir: Any,
    store_configured: bool = True,
) -> Any:
    """The library normalizer wired as the composition root does, with no work lock."""
    from contextlib import nullcontext

    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.infrastructure.packaging.cbz import PageTarget
    from shelfhold.library.public import KeepCopier, LibraryPageNormalizer

    keeper = KeepCopier(
        engine, fsops, locate, store, data, IntentJournal(engine, fsops),
        hold_work=lambda _work: nullcontext(),
    )  # fmt: skip
    return LibraryPageNormalizer(
        engine, fsops, locate, keeper, workdir, lambda _work: nullcontext(),
        target=PageTarget(), store_configured=store_configured, store_dir=store,
    )  # fmt: skip
