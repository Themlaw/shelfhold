"""Library and tombstones: codes of EF-BIB-053 / EF-BIB-054 and the safeguard queue."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("series.stale", 409, "admin", event=True),
    ErrorCode("series.tombstoned_upstream", 409, "admin", event=True),
    ErrorCode("tombstone.unverified", 409, "admin", event=True),
    ErrorCode("tombstone.migration_hit", 409, "admin", event=True),
    ErrorCode("tombstone.key_unknown", 409, "admin", event=True),
    ErrorCode("tombstone.root_mismatch", 409, "admin", event=True),
    ErrorCode("unit.safeguard_failed", 500, "admin", event=True),
    ErrorCode("unit.confirmation_required", 409, "user"),
    ErrorCode("unit.safeguard_backlog", 503, "admin", event=True),
    ErrorCode("series.gap", 409, "user", event=True),
    ErrorCode("tombstone.journal_truncated", 409, "admin", event=True),
)
