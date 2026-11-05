"""ENF-SEC-003: Image.open only on the allowlist (never EPS), exif_transpose only in the worker."""

from __future__ import annotations

import ast
import io
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image
from shelfhold.infrastructure.imaging import (
    ImageKind,
    ImageRejected,
    make_thumbnail,
    probe_image,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
WORKER = SRC / "infrastructure" / "imaging" / "worker.py"


@pytest.mark.req("ENF-SEC-003")
def test_image_open_refuses_eps_and_unlisted_formats(malicious: ModuleType) -> None:
    samples = malicious.unlisted_images()
    assert {"eps", "eps_dos", "bmp", "tiff", "ppm", "pdf", "svg", "html"} <= set(samples)
    for name, data in samples.items():
        for kind in (ImageKind.COVER, ImageKind.PAGE, ImageKind.WEBTOON):
            with pytest.raises(ImageRejected) as caught:
                probe_image(data, kind)
            assert caught.value.code in {"image.format_refused", "image.decode_failed"}, name
        if name in {"eps", "eps_dos", "bmp", "ppm"}:
            # Pillow would open these: it is the allowlist that says no, with the stable code
            with pytest.raises(ImageRejected) as caught:
                probe_image(data, ImageKind.PAGE)
            assert caught.value.code == "image.format_refused", name


@pytest.mark.req("ENF-SEC-003")
def test_image_open_every_call_passes_a_formats_list() -> None:
    """Static: in the whole backend, Image.open is called only in the worker, with formats=."""
    offenders: list[str] = []
    calls = 0
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = node.func
            is_open = target.attr == "open" and getattr(target.value, "id", "") in {"Image", "PIL"}
            is_open |= (
                target.attr == "open"
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "Image"
            )
            if not is_open:
                continue
            calls += 1
            if path != WORKER or "formats" not in {kw.arg for kw in node.keywords}:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert calls >= 1
    assert offenders == []


@pytest.mark.req("ENF-SEC-003")
def test_exif_transpose_is_never_called_outside_the_worker() -> None:
    mentions = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "exif_transpose" in p.read_text(encoding="utf-8") and p != WORKER
    ]
    assert mentions == []
    assert "exif_transpose" in WORKER.read_text(encoding="utf-8")


@pytest.mark.req("ENF-SEC-003")
def test_pillow_is_imported_only_by_the_worker() -> None:
    importers = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(n == "PIL" or n.startswith("PIL.") for n in names):
                importers.append(path)
    assert set(importers) == {WORKER}


def _encoded(fmt: str, size: tuple[int, int] = (40, 20), **save: object) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, fmt, **save)
    return out.getvalue()


@pytest.mark.req("ENF-SEC-003")
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_image_open_accepts_every_allowlisted_format(fmt: str) -> None:
    info = probe_image(_encoded(fmt), ImageKind.PAGE)
    assert (info.format, info.width, info.height) == (fmt, 40, 20)


@pytest.mark.req("ENF-SEC-003")
def test_thumbnail_applies_exif_orientation_inside_the_worker() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    jpeg = _encoded("JPEG", (80, 40), exif=exif.tobytes())
    thumb = make_thumbnail(jpeg, ImageKind.COVER, max_edge=32, fmt="png")
    assert (thumb.width, thumb.height) == (16, 32)  # portrait after the transpose
    decoded = Image.open(io.BytesIO(thumb.data))
    assert decoded.size == (16, 32)
    assert not decoded.getexif()  # no EXIF is carried into the derivative


@pytest.mark.req("ENF-SEC-003")
def test_thumbnail_output_is_bounded_and_reencoded(malicious: ModuleType) -> None:
    thumb = make_thumbnail(malicious.png_solid(600, 400), ImageKind.PAGE, max_edge=100, fmt="webp")
    assert max(thumb.width, thumb.height) == 100
    assert thumb.format == "WEBP"
    with pytest.raises(ValueError, match="max_edge"):
        make_thumbnail(malicious.png_solid(10, 10), ImageKind.PAGE, max_edge=0)
    with pytest.raises(ValueError, match="fmt"):
        make_thumbnail(malicious.png_solid(10, 10), ImageKind.PAGE, max_edge=10, fmt="eps")
