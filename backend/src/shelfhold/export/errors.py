"""Export targets (Kavita): failure matrix codes of EF-EXP-009 / ENF-OPS-011."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("target.unreachable", 503, "admin"),
    ErrorCode("target.forbidden", 403, "admin"),
    ErrorCode("target.not_indexed", 409, "admin"),
    ErrorCode("target.version_unsupported", 503, "admin"),
    ErrorCode("target.route_missing", 503, "admin"),
    ErrorCode("target.instance_changed", 409, "admin"),
    ErrorCode("target.key_expiring", 409, "admin"),
    ErrorCode("target.foreign_writer_detected", 409, "admin"),
    ErrorCode("target.permission_denied", 403, "admin"),
    ErrorCode("target.child_exposure", 409, "admin"),
)
