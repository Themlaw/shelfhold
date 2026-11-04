"""ETag and `If-Match` (EF-INT-001): 412 when stale, 428 when missing, 409 stays business-only."""

from shelfhold.interfaces.http_api.problems import ProblemError


def etag_for_version(version: int | str) -> str:
    """Strong validator derived from a version counter or a content hash."""
    text = str(version)
    if not text or '"' in text or any(ord(c) < 0x21 or ord(c) > 0x7E for c in text):
        raise ValueError("invalid_etag_value")
    return f'"{text}"'


def _candidates(header: str) -> list[str]:
    return [part.strip() for part in header.split(",") if part.strip()]


def check_if_match(header: str | None, current_etag: str) -> None:
    """Refuse the write unless the caller saw the current version (RFC 9110 §13.1.1).

    `*` matches any existing resource; weak validators (`W/`) never match: a write needs the
    strong comparison.
    """
    if header is None:
        raise ProblemError("precondition_required")
    candidates = _candidates(header)
    if "*" in candidates or current_etag in candidates:
        return
    raise ProblemError("precondition_failed")
