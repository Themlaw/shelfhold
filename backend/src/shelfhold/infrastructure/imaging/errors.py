"""Stable refusal of an image (the codes are in the error catalog)."""

from __future__ import annotations

from typing import Final

from shelfhold.error_catalog import ErrorCode

TOO_LARGE: Final = "image.too_large"
FORMAT_REFUSED: Final = "image.format_refused"
DECODE_FAILED: Final = "image.decode_failed"
WORKER_FAILED: Final = "image.worker_failed"


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
)
