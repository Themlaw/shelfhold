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
    ErrorCode("export.segment_policy_unconfirmed", 409, "admin"),
    # L27: planner and bindings (EF-EXP-002, EF-EXP-032, EF-EXP-058, EF-EXP-062)
    ErrorCode("shelf.parental_control_unavailable", 422, "user"),
    ErrorCode("target.tree_overlap", 409, "admin"),
    ErrorCode("shelf.destination_exists", 409, "admin"),
    ErrorCode("target.pull_needs_readable_copy", 409, "admin"),
    ErrorCode("export.watermarked_external_egress", 409, "user"),
    ErrorCode("export.series_name_collision", 409, "admin"),
    ErrorCode("export.no_remote_library", 409, "admin"),
    ErrorCode("export.transport_unsupported", 409, "admin"),
    ErrorCode("export.destination_occupied", 409, "admin"),
    ErrorCode("export.source_missing", 409, "admin"),
    # L30: pairing with the Suwayomi follow, switchover (EF-EXP-038, EF-EXP-044, EF-EXP-054)
    ErrorCode("pairing.switchover_unconfirmed", 409, "admin"),
    ErrorCode("pairing.not_writable", 409, "admin"),
    ErrorCode("pairing.already_paired", 409, "admin"),
    ErrorCode("pairing.batch_stale", 409, "admin"),
    ErrorCode("pairing.switchover_stale", 409, "admin"),
)
