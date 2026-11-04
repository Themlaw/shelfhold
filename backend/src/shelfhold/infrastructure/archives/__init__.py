"""The only place that opens archives (safe_open_archive, ENF-SEC-002); see importlinter.ini."""

from shelfhold.infrastructure.archives.safe_open import (
    DEFAULT_LIMITS,
    ArchiveEntry,
    ArchiveLimits,
    EntryStream,
    SafeArchive,
    UnsafeArchiveError,
    safe_open_archive,
)

__all__ = [
    "DEFAULT_LIMITS",
    "ArchiveEntry",
    "ArchiveLimits",
    "EntryStream",
    "SafeArchive",
    "UnsafeArchiveError",
    "safe_open_archive",
]
