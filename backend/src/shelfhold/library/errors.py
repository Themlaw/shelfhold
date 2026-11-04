"""Library and tombstones: codes of EF-BIB-053 / EF-BIB-054 and the safeguard queue."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("series.stale", 409, "admin"),
    ErrorCode("series.tombstoned_upstream", 409, "admin"),
    ErrorCode("tombstone.unverified", 409, "admin"),
    ErrorCode("tombstone.migration_hit", 409, "admin"),
    ErrorCode("tombstone.key_unknown", 409, "admin"),
    ErrorCode("tombstone.root_mismatch", 409, "admin"),
    ErrorCode("unit.safeguard_failed", 500, "admin"),
    ErrorCode("unit.safeguard_backlog", 503, "admin"),
)
