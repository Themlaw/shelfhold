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


class TombstonedContentError(PermissionError):
    """The file's content matches a tombstone (EF-BIB-052): it is never filed again, whatever road
    it came by (follow, "fill the gaps", import, adoption). Nothing was placed."""

    code = "unit.tombstoned"


class EpubRefusedError(ValueError):
    """The neutralizer refused an `untrusted` EPUB (an entry that is not on its allowlist, an
    unsafe archive or XML): there is no derivative and nothing is exported (EF-BIB-051). `reason`
    is the stable word of the refusal, `entry` the archive entry it is about, if any."""

    code: ClassVar[str] = "content.untrusted_entry"

    def __init__(self, reason: str, entry: str | None = None) -> None:
        super().__init__(f"{self.code}: {reason}" + (f" ({entry})" if entry else ""))
        self.reason = reason
        self.entry = entry


class OriginalNotServableError(PermissionError):
    """What was asked to leave Shelfhold is not the neutralized derivative of an `untrusted` EPUB
    (none exists, or the file is not the one recorded). The original is never a way out
    (EF-BIB-051, EF-BIB-006): `reason` says which case."""

    code: ClassVar[str] = "content.original_not_servable"

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(f"{self.code}: {reason}" + (f" ({message})" if message else ""))
        self.reason = reason


class RegenerationBlockedError(RuntimeError):
    """Neither the original nor the stored derivative verifies: nothing is regenerated."""

    code: ClassVar[str] = "content.regeneration_blocked"

    def __init__(self, reason: str) -> None:
        super().__init__(f"{self.code}: {reason}")
        self.reason = reason
