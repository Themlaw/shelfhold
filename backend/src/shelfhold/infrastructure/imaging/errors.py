"""Stable refusal of an image (the codes are in the error catalog)."""

from __future__ import annotations

from typing import Final

from shelfhold.error_catalog import ErrorCode

TOO_LARGE: Final = "image.too_large"
FORMAT_REFUSED: Final = "image.format_refused"
DECODE_FAILED: Final = "image.decode_failed"
WORKER_FAILED: Final = "image.worker_failed"

# The page normalization (M4 L53, ADR-0054): what is said of a page that could not be brought to
# an allowlisted format, and why `library normalize-pages` left a Unit as it was. A closed list.
PAGE_EMPTY: Final = "page.empty"
PAGE_UNDECODABLE: Final = "page.undecodable"
PAGE_TOO_LARGE: Final = "page.too_large"
PAGE_FORMAT_UNSUPPORTED: Final = "page.format_unsupported"
PAGES_STORE_NOT_CONFIGURED: Final = "pages.store_not_configured"
PAGES_WORK_BUSY: Final = "pages.work_busy"
PAGES_NOT_KEPT: Final = "pages.not_kept"
PAGES_WORKER_FAILED: Final = "pages.worker_failed"
PAGES_UNREADABLE: Final = "pages.unreadable"


class ImageRejected(ValueError):
    """An image was refused. `code` is stable; `reason` and `stage` are for logs and tests.

    `stage` is where it stopped: `input` (before the worker), `header` (size read, no pixel
    decoded), `decode` or `worker`.
    """

    def __init__(self, code: str, reason: str, stage: str) -> None:
        super().__init__(f"{code}: {reason} ({stage})")
        self.code = code
        self.reason = reason
        self.stage = stage


CODES: tuple[ErrorCode, ...] = (
    ErrorCode(TOO_LARGE, 422, "user"),
    ErrorCode(FORMAT_REFUSED, 422, "user"),
    ErrorCode(DECODE_FAILED, 422, "user"),
    ErrorCode(WORKER_FAILED, 500, "admin"),
    ErrorCode(PAGE_EMPTY, 422, "user"),
    ErrorCode(PAGE_UNDECODABLE, 422, "user"),
    ErrorCode(PAGE_TOO_LARGE, 422, "user"),
    ErrorCode(PAGE_FORMAT_UNSUPPORTED, 422, "user"),
    ErrorCode(PAGES_STORE_NOT_CONFIGURED, 409, "admin"),
    ErrorCode(PAGES_WORK_BUSY, 409, "admin"),
    ErrorCode(PAGES_NOT_KEPT, 409, "admin"),
    ErrorCode(PAGES_WORKER_FAILED, 500, "admin"),
    ErrorCode(PAGES_UNREADABLE, 422, "admin"),
)
