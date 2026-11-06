"""ETag (EF-INT-001). `If-Match` is read by the route that needs it (plans: one strong validator,
412 when stale, 428 when missing, 409 stays business-only)."""


def etag_for_version(version: int | str) -> str:
    """Strong validator derived from a version counter or a content hash."""
    text = str(version)
    if not text or '"' in text or "," in text or any(ord(c) < 0x21 or ord(c) > 0x7E for c in text):
        raise ValueError("invalid_etag_value")
    return f'"{text}"'
