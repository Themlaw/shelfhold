"""Library and tombstones: codes of EF-BIB-053 / EF-BIB-054 and the safeguard queue."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("content.untrusted_entry", 422, "admin", event=True),  # EF-BIB-051 (6)
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
    # The import folder (EF-SRC-012): each answers a file that stays where it is
    ErrorCode("content.untrusted_pdf", 409, "user"),  # D9: kept, not exported, shown in "À ranger"
    ErrorCode("import.not_configured", 409, "admin"),  # no `import_dir`
    ErrorCode("import.protected", 422, "user"),
    ErrorCode("import.protected_licence", 422, "user"),
    ErrorCode("import.pdf_refused", 422, "user"),
    ErrorCode("import.audio_unsupported", 422, "user"),
    ErrorCode("import.unsupported_file", 422, "user"),
    ErrorCode("import.too_large", 422, "user"),
    ErrorCode("import.archive_refused", 422, "user"),
    ErrorCode("import.link_refused", 422, "user"),
    ErrorCode("import.unsafe_name", 422, "user"),
    ErrorCode("import.name_collision", 422, "user"),
    ErrorCode("import.not_ready", 409, "user"),
    ErrorCode("import.left_in_quarantine", 409, "admin"),
    ErrorCode("import.purchase_pending", 409, "user"),
    ErrorCode("import.changed_while_reading", 409, "user"),
    ErrorCode("import.hash_mismatch", 500, "admin"),
    ErrorCode("import.disk_full", 507, "admin"),
    ErrorCode("import.not_stored", 500, "admin"),
    ErrorCode("import.tombstoned", 409, "user"),
    ErrorCode("import.own_export", 409, "user"),  # Shelfhold's own export coming back (I50)
    ErrorCode("import.original_not_kept", 409, "admin"),
    # An existing Suwayomi download tree (EF-INT-009): a chapter left alone, or a tree not read
    ErrorCode("import.tree_unreadable", 422, "admin"),
    ErrorCode("import.tree_no_series", 409, "user"),
    ErrorCode("import.tree_ambiguous_series", 409, "user"),
    ErrorCode("import.tree_unnumbered", 409, "user"),
    ErrorCode("import.tree_duplicate", 409, "user"),
    ErrorCode("import.tree_wrong_source", 409, "user"),
    ErrorCode("import.tree_needs_rating", 409, "admin"),
)
