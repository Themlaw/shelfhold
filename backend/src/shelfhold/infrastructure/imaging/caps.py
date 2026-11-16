"""Image kinds, their pixel and size caps, and the format allowlist (ENF-SEC-003)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# Pillow's own default for Image.MAX_IMAGE_PIXELS, which the cahier des charges names ("89 Mpx").
PILLOW_DEFAULT_MAX_PIXELS: Final = 89_478_485
WEBTOON_MAX_PIXELS: Final = 250_000_000
WEBTOON_MAX_HEIGHT: Final = 60_000

# Pillow format names, in the order they are tried. Never EPS (it hands the file to Ghostscript),
# never anything off this list: BMP, TIFF, PPM, ICO, PSD, PDF, SVG... Pillow ignores a name it has
# no plugin for (JXL needs a third-party plugin), so listing it is harmless.
ALLOWED_FORMATS: Final = ("JPEG", "PNG", "WEBP", "GIF", "AVIF", "JXL")
# Formats a page may be *read* in to be re-encoded (M4 L53, ADR-0054): only the `page_input` kind
# lists them. HEIC needs a third-party plugin Pillow does not ship (refused with a closed code).
CONVERTIBLE_FORMATS: Final = ("BMP", "TIFF", "JPEG2000")
PAGE_INPUT_FORMATS: Final = ALLOWED_FORMATS + CONVERTIBLE_FORMATS


class ImageKind(StrEnum):
    """What the image is for: the unit of the caps."""

    MANGA = "manga"
    COMIC = "comic"
    BOOK = "book"
    WEBTOON = "webtoon"
    COVER = "cover"
    PAGE = "page"
    PAGE_INPUT = "page_input"  # a page of any format the worker can re-encode
    THUMBNAIL = "thumbnail"


@dataclass(frozen=True, slots=True)
class ImageCaps:
    """The limits one image must respect. `max_height` is None when only the pixel count counts."""

    max_pixels: int
    max_bytes: int
    max_height: int | None = None
    formats: tuple[str, ...] = ALLOWED_FORMATS
    convertible: bool = False  # may list CONVERTIBLE_FORMATS (the `page_input` kind only)

    def __post_init__(self) -> None:
        if self.max_pixels <= 0 or self.max_bytes <= 0:
            raise ValueError("caps must be positive")
        if self.max_height is not None and self.max_height <= 0:
            raise ValueError("max_height must be positive")
        allowed = PAGE_INPUT_FORMATS if self.convertible else ALLOWED_FORMATS
        unlisted = {name for name in self.formats if name not in allowed}
        if unlisted:  # EPS (Ghostscript) and every other format Pillow could open
            raise ValueError(f"formats outside the allowlist: {sorted(unlisted)}")


_MIB = 1024 * 1024

DEFAULT_CAPS: Final[dict[ImageKind, ImageCaps]] = {
    ImageKind.MANGA: ImageCaps(PILLOW_DEFAULT_MAX_PIXELS, 128 * _MIB),
    ImageKind.COMIC: ImageCaps(PILLOW_DEFAULT_MAX_PIXELS, 128 * _MIB),
    ImageKind.BOOK: ImageCaps(PILLOW_DEFAULT_MAX_PIXELS, 128 * _MIB),
    ImageKind.PAGE: ImageCaps(PILLOW_DEFAULT_MAX_PIXELS, 128 * _MIB),
    ImageKind.PAGE_INPUT: ImageCaps(
        PILLOW_DEFAULT_MAX_PIXELS, 128 * _MIB, formats=PAGE_INPUT_FORMATS, convertible=True
    ),
    ImageKind.WEBTOON: ImageCaps(WEBTOON_MAX_PIXELS, 256 * _MIB, WEBTOON_MAX_HEIGHT),
    ImageKind.COVER: ImageCaps(40_000_000, 64 * _MIB),
    ImageKind.THUMBNAIL: ImageCaps(16_000_000, 32 * _MIB),
}


def caps_for(kind: ImageKind | str) -> ImageCaps:
    return DEFAULT_CAPS[ImageKind(kind)]
