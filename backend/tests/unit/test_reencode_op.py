"""M4 L53 (ADR-0054): the `reencode` op of the decode worker, called in-process (in production it
runs in a sandboxed subprocess; the sandbox itself is tested elsewhere)."""

import io

import pytest
from PIL import Image

from shelfhold.infrastructure.imaging import worker
from shelfhold.infrastructure.imaging.caps import DEFAULT_CAPS, ImageKind


def caps() -> dict:
    c = DEFAULT_CAPS[ImageKind.PAGE_INPUT]
    return {
        "max_pixels": c.max_pixels, "max_bytes": c.max_bytes, "max_height": None,
        "formats": list(c.formats), "convertible": True,
    }  # fmt: skip


def page(mode: str, fmt: str, colour: object) -> bytes:
    out = io.BytesIO()
    Image.new(mode, (20, 10), colour).save(out, fmt)
    return out.getvalue()


@pytest.mark.req("EF-EXP-046")
@pytest.mark.parametrize(
    ("fmt", "mode", "colour", "expected"),
    [
        ("jpeg", "RGBA", (255, 0, 0, 0), "JPEG"),
        ("png", "P", 3, "PNG"),
        ("webp", "RGB", "red", "WEBP"),
        ("jpeg", "L", 128, "JPEG"),
        ("png", "RGBA", (0, 0, 0, 10), "PNG"),
    ],
)
def test_reencode_gives_the_target_format(
    fmt: str, mode: str, colour: object, expected: str
) -> None:
    source = page(mode, "BMP" if mode in {"RGB", "L"} else "PNG", colour)
    meta, data = worker._reencode(source, {"fmt": fmt, "quality": 80, "caps": caps()})
    assert meta["format"] == expected and (meta["width"], meta["height"]) == (20, 10)
    assert Image.open(io.BytesIO(data)).format == expected


@pytest.mark.req("EF-EXP-046")
def test_reencode_flattens_transparency_on_white() -> None:
    source = page("RGBA", "PNG", (255, 0, 0, 0))
    _, data = worker._reencode(source, {"fmt": "jpeg", "quality": 90, "caps": caps()})
    r, g, b = Image.open(io.BytesIO(data)).convert("RGB").getpixel((5, 5))
    assert min(r, g, b) >= 245
