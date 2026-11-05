"""Typed failures of the library context; `code` is the stable catalogue code (EF-UX-003)."""

from typing import ClassVar


class ReceivedTooLargeError(ValueError):
    """A received archive is above the bound (`received_cbz_max_bytes`), checked before opening."""

    code: ClassVar[str] = "fetch.too_large"


class SafeguardFailedError(RuntimeError):
    """The copy to /store was not made, and nothing was written. `reason` is a stable word."""

    code: ClassVar[str] = "unit.safeguard_failed"

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(f"{self.code}: {reason}" + (f" ({message})" if message else ""))
        self.reason = reason


class VariantAlreadyStoredError(FileExistsError):
    """The Variant already has its archive: the same release arrived twice (a replay is a no-op)."""
