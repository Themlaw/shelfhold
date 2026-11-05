"""ENF-SEC-003: pixel caps per kind, bombs refused at the header, formats on a list."""

from __future__ import annotations

from types import ModuleType

import pytest
from PIL import Image
from shelfhold.infrastructure.imaging import (
    ALLOWED_FORMATS,
    DEFAULT_CAPS,
    ImageCaps,
    ImageKind,
    ImageRejected,
    caps_for,
    probe_image,
)

PILLOW_DEFAULT_MAX_PIXELS = 89_478_485


@pytest.mark.req("ENF-SEC-003")
def test_image_caps_defaults_per_kind() -> None:
    for kind in (ImageKind.MANGA, ImageKind.COMIC, ImageKind.BOOK):
        assert caps_for(kind).max_pixels == PILLOW_DEFAULT_MAX_PIXELS
    webtoon = caps_for(ImageKind.WEBTOON)
    assert webtoon.max_pixels == 250_000_000
    assert webtoon.max_height == 60_000
    # every kind has caps, every cap is finite, and no kind may open more than the webtoon
    assert set(DEFAULT_CAPS) == set(ImageKind)
    for caps in DEFAULT_CAPS.values():
        assert 0 < caps.max_pixels <= webtoon.max_pixels
        assert caps.max_bytes > 0
        assert caps.formats == ALLOWED_FORMATS
    # cover, page and thumbnail have their own, tighter-or-equal, caps
    assert caps_for(ImageKind.COVER).max_pixels <= caps_for(ImageKind.PAGE).max_pixels
    assert caps_for(ImageKind.THUMBNAIL).max_pixels <= caps_for(ImageKind.COVER).max_pixels


@pytest.mark.req("ENF-SEC-003")
def test_image_caps_allowlist_has_no_eps_and_no_exotic_format() -> None:
    assert set(ALLOWED_FORMATS) <= {"JPEG", "PNG", "WEBP", "GIF", "AVIF", "JXL"}
    assert "EPS" not in {f.upper() for f in ALLOWED_FORMATS}


@pytest.mark.req("ENF-SEC-003")
def test_image_bomb_caps_per_kind(malicious: ModuleType) -> None:
    # 90.25 Mpx: over the manga cap (header check, no decode), under the webtoon cap (decoded)
    bomb = malicious.png_solid(9_500, 9_500)
    for kind in (ImageKind.MANGA, ImageKind.COMIC, ImageKind.BOOK):
        with pytest.raises(ImageRejected) as caught:
            probe_image(bomb, kind)
        assert caught.value.code == "image.too_large"
        assert caught.value.reason == "pixels"
        assert caught.value.stage == "header"
    info = probe_image(bomb, ImageKind.WEBTOON)
    assert (info.width, info.height, info.format) == (9_500, 9_500, "PNG")

    # a size lie (header claims 144 Mpx, no pixel data) is refused before any pixel is touched
    lie = malicious.png_header_only(12_000, 12_000)
    with pytest.raises(ImageRejected) as caught:
        probe_image(lie, ImageKind.MANGA)
    assert (caught.value.reason, caught.value.stage) == ("pixels", "header")

    # webtoon height cap: 60 000 px high is fine, 60 001 is not
    tall = malicious.png_solid(8, 60_000)
    assert probe_image(tall, ImageKind.WEBTOON).height == 60_000
    with pytest.raises(ImageRejected) as caught:
        probe_image(malicious.png_solid(8, 60_001), ImageKind.WEBTOON)
    assert (caught.value.code, caught.value.reason) == ("image.too_large", "height")
    # ... and the height cap belongs to webtoons: a manga page may be taller than that
    assert probe_image(tall, ImageKind.MANGA).height == 60_000


@pytest.mark.req("ENF-SEC-003")
def test_image_caps_are_overridable_per_call(malicious: ModuleType) -> None:
    png = malicious.png_solid(100, 100)
    tight = ImageCaps(max_pixels=9_999, max_bytes=10_000_000, max_height=None)
    with pytest.raises(ImageRejected) as caught:
        probe_image(png, ImageKind.PAGE, caps=tight)
    assert caught.value.reason == "pixels"
    roomy = ImageCaps(max_pixels=10_000, max_bytes=10_000_000, max_height=None)
    assert probe_image(png, ImageKind.PAGE, caps=roomy).width == 100


@pytest.mark.req("ENF-SEC-003")
def test_image_byte_cap_is_checked_before_the_worker_is_started(malicious: ModuleType) -> None:
    png = malicious.png_solid(100, 100)
    tiny = ImageCaps(max_pixels=1_000_000, max_bytes=len(png) - 1, max_height=None)
    with pytest.raises(ImageRejected) as caught:
        probe_image(png, ImageKind.PAGE, caps=tiny)
    assert (caught.value.code, caught.value.reason, caught.value.stage) == (
        "image.too_large",
        "bytes",
        "input",
    )


@pytest.mark.req("ENF-SEC-003")
def test_image_truncated_data_is_a_decode_failure_not_a_crash(malicious: ModuleType) -> None:
    png = malicious.png_solid(200, 200)
    with pytest.raises(ImageRejected) as caught:
        probe_image(png[: len(png) // 2], ImageKind.PAGE)
    assert caught.value.code == "image.decode_failed"


@pytest.mark.req("ENF-SEC-003")
def test_image_pillow_global_cap_is_never_disabled() -> None:
    # importing the parent side must not touch Pillow's own guard
    assert Image.MAX_IMAGE_PIXELS == PILLOW_DEFAULT_MAX_PIXELS
