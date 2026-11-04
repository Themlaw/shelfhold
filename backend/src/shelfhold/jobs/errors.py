"""Job ledger failures: `error_code` values written by the Huey adapter and the window scheduler."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("job.unknown_kind", 500, "admin"),
    ErrorCode("job.failed", 500, "admin"),
    ErrorCode("window.preempted", 503, "admin"),
)
