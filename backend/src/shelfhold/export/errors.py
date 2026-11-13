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
    ErrorCode("target.library_type_changed", 409, "admin", event=True),  # EF-EXP-049
    # 412, not 409: 409 stays for business conflicts (EF-EXP-042, If-Match semantics)
    ErrorCode("plan.stale", 412, "user"),
    # EF-EXP-045: a name `plugin/parse` would misread is refused, never written
    ErrorCode("export.series_name_yields_volume", 422, "user"),
    ErrorCode("export.name_misread_number", 422, "user"),
    ErrorCode("export.segment_policy_unconfirmed", 409, "admin"),
    # L27: planner and bindings (EF-EXP-002, EF-EXP-032, EF-EXP-058, EF-EXP-062)
    ErrorCode("shelf.ceiling_invalid", 422, "user"),
    # M3 L22 (EF-EXP-062): the plan that moves an exported series is stale or has blockers
    ErrorCode("export.series_reassignment_refused", 409, "admin"),
    ErrorCode("target.tree_overlap", 409, "admin"),
    ErrorCode("shelf.destination_exists", 409, "admin"),
    ErrorCode("target.pull_needs_readable_copy", 409, "admin"),
    ErrorCode("export.watermarked_external_egress", 409, "user"),
    ErrorCode("export.series_name_collision", 409, "admin"),
    ErrorCode("export.no_remote_library", 409, "admin"),
    ErrorCode("export.transport_unsupported", 409, "admin"),
    ErrorCode("export.destination_occupied", 409, "admin"),
    ErrorCode("export.source_missing", 409, "admin"),
    # L10: family shelves (EF-USR-015, EF-EXP-047, EF-BIB-037, EF-BIB-055)
    ErrorCode("shelf.rating_required", 422, "user"),
    ErrorCode("shelf.above_ceiling", 422, "user"),
    ErrorCode("shelf.suggestion_above", 422, "user"),
    ErrorCode("shelf.family_ceiling_too_high", 422, "user"),
    ErrorCode("shelf.destination_not_partitioned", 409, "admin"),
    ErrorCode("shelf.library_not_dedicated", 409, "admin"),
    ErrorCode("target.remote_folder_shared", 409, "admin"),
    ErrorCode("export.series_other_shelf", 409, "admin"),
    ErrorCode("job.busy", 409, "user"),
    ErrorCode("shelf.withdrawal_root_read_only", 409, "admin"),
    ErrorCode("shelf.not_child_library", 409, "admin"),
    ErrorCode("export.age_tier_mix", 409, "admin"),
    # L30: pairing with the Suwayomi follow, switchover (EF-EXP-038, EF-EXP-044, EF-EXP-054)
    ErrorCode("pairing.switchover_unconfirmed", 409, "admin"),
    ErrorCode("pairing.not_writable", 409, "admin"),
    ErrorCode("pairing.already_paired", 409, "admin"),
    ErrorCode("pairing.batch_stale", 409, "admin"),
    ErrorCode("pairing.switchover_stale", 409, "admin"),
    # L31: repatriation before unmounting a root (EF-EXP-051, EF-EXP-060)
    ErrorCode("repatriate.dry_run_required", 409, "admin"),
    ErrorCode("repatriate.stale", 409, "admin"),
    ErrorCode("repatriate.blocked", 409, "admin"),
    ErrorCode("repatriate.no_space", 507, "admin"),
    # L16 (M2): a served file replaced at its path; KOReader starts it again (EF-EXP-016, D21)
    ErrorCode("unit.upgraded", 409, "user", event=True),
    # L09 (M2): EPUB series in a Kavita Books library (EF-EXP-040, D19)
    ErrorCode("export.awaiting_series_age", 409, "admin"),
    ErrorCode("target.library_type_mismatch", 409, "admin"),
    ErrorCode("export.series_age_stale", 409, "admin"),
)
