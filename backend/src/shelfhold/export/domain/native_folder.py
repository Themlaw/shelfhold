"""What the host and the native folder destination say to each other (EF-EXP-010).

The cover of a series is the first page of one of its items; the plan cites it as a `sidecar`
reference, and the host (which serves the bytes) reads the item back out of it. The rule lives in
the domain so the application and the destination share it without importing each other.
"""

_COVER_PREFIX = "cover:"


def cover_ref(item_id: str) -> str:
    """The `sidecar` reference of the cover of a series: the first page of this item."""
    return f"{_COVER_PREFIX}{item_id}"


def cover_item_of(ref: str) -> str | None:
    """The item a cover reference names, or None when it is not one."""
    return ref[len(_COVER_PREFIX) :] if ref.startswith(_COVER_PREFIX) else None


COVER_STEM = "cover"  # Mihon finds a file named `cover.*`, then reads the image by its content
COVER_EXTENSIONS = ("jpg", "png", "webp")  # what a reader that trusts the extension takes


def cover_extension(image: bytes) -> str | None:
    """The extension of an image by its first bytes (never by a name): jpg, png or webp, None for
    any other format (AVIF, JXL, GIF...), which is converted before it is written."""
    if image.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "webp"
    return None
