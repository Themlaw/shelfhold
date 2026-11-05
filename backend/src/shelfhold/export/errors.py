"""Export targets (Kavita): failure matrix codes of EF-EXP-009 / ENF-OPS-011."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("target.unreachable", 503, "admin", event=True),
    ErrorCode("target.forbidden", 403, "admin", event=True),
    ErrorCode("target.not_indexed", 409, "admin", event=True),
    ErrorCode("target.version_unsupported", 503, "admin", event=True),
    ErrorCode("target.route_missing", 503, "admin", event=True),
    ErrorCode("target.instance_changed", 409, "admin", event=True),
    ErrorCode("target.key_expiring", 409, "admin", event=True),
    ErrorCode("target.foreign_writer_detected", 409, "admin", event=True),
    ErrorCode("target.permission_denied", 403, "admin", event=True),
    ErrorCode("target.child_exposure", 409, "admin", event=True),
    # 412, not 409: 409 stays for business conflicts (EF-EXP-042, If-Match semantics)
    ErrorCode("plan.stale", 412, "user"),
    # EF-EXP-045: a name `plugin/parse` would misread is refused, never written
    ErrorCode("export.series_name_yields_volume", 422, "user"),
    ErrorCode("export.name_misread_number", 422, "user"),
)
