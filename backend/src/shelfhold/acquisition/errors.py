"""Sources and the guarded Fetcher: SDK typed errors, SSRF guard, transport failures."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("source.error", 502, "admin"),
    ErrorCode("source.broken", 502, "admin", event=True),
    ErrorCode("source.auth_required", 401, "admin"),
    ErrorCode("source.auth_expired", 503, "admin", event=True),
    ErrorCode("source.rate_limited", 503, "admin"),
    ErrorCode("source.content_removed", 410, "user"),
    ErrorCode("source.changed", 502, "admin"),
    ErrorCode("source.temporarily_unavailable", 503, "admin"),
    ErrorCode("source.version_unsupported", 503, "admin", event=True),
    # EF-SRC-035: a reference handed back by a source holds a secret or a userinfo part
    ErrorCode("source.ref_has_secret", 502, "admin", event=True),
    ErrorCode("source.not_loaded", 503, "admin", event=True),
    ErrorCode("source.downloads_dropped", 503, "admin", event=True),
    ErrorCode("source.auth_mode_unsupported", 503, "admin", event=True),
    ErrorCode("source.instance_changed", 409, "admin", event=True),
    # EF-SRC-026 (d): the global update settings of the tool leave out the series that are followed
    ErrorCode("source.update_filters_blocking", 409, "admin"),
    # EF-SRC-004: a pasted URL that no active plugin recognises
    ErrorCode("source.no_match", 422, "user"),
    # EF-SRC-015 (M2 L19): one-shot acquisition from a native source
    ErrorCode("source.filter_invalid", 422, "user"),  # a search filter the source does not declare
    ErrorCode("acquire.unsupported_file", 422, "user"),  # a PDF, audio, or no file at all
    ErrorCode("acquire.unit_required", 422, "user"),  # several Units offered, none chosen
    ErrorCode("acquire.series_tombstoned", 409, "user"),  # the series was deleted on purpose
    ErrorCode("unit.stored", 409, "user", event=True),  # "rangé": the file is in the library
    # EF-SRC-013: a per-source setting refused (the field and a reason, never the value)
    ErrorCode("source.setting_invalid", 422, "admin"),
    # EF-SRC-013, EF-EXP-065: no loaded plugin, or no installed third-party plugin, by that key
    ErrorCode("plugin.unknown", 404, "admin"),
    # EF-EXP-065 (5): only a declared trust value of a third-party plugin is attested
    ErrorCode("plugin.attestation_invalid", 422, "admin"),
    ErrorCode("plugin.attestation_not_needed", 409, "admin"),
    # EF-SRC-003: a plugin's Fetcher asked for a host outside its DomainRules
    ErrorCode("source.domain_not_allowed", 403, "admin"),
    # EF-SRC-002: a plugin did network, disk or process I/O itself (the audit-hook detector)
    ErrorCode("source.forbidden_io", 403, "admin"),
    ErrorCode("source.dns_blocked", 502, "admin", event=True),
    ErrorCode("ssrf.blocked", 403, "admin"),
    ErrorCode("fetch.error", 502, "admin"),
    ErrorCode("fetch.dns_failed", 502, "admin"),
    ErrorCode("fetch.invalid_url", 422, "user"),
    ErrorCode("fetch.too_many_redirects", 502, "admin"),
    ErrorCode("fetch.unsafe_redirect", 502, "admin"),
    ErrorCode("fetch.too_large", 502, "admin"),
    ErrorCode("fetch.robots_disallowed", 403, "admin"),  # ENF-LEG-003
    ErrorCode("fetch.robots_unavailable", 503, "admin"),  # ENF-LEG-003
    ErrorCode("fetch.quota_exceeded", 429, "admin"),  # ENF-LEG-005
    ErrorCode("fetch.user_agent_not_identifiable", 500, "admin"),  # ENF-LEG-005
    ErrorCode("fetch.timeout", 504, "admin"),
    ErrorCode("fetch.unreachable", 502, "admin"),
    ErrorCode("source.fallback", 409, "admin", event=True),
    # EF-SRC-009: series of the Suwayomi library wait for an adult shelf to be filed on
    ErrorCode("series.awaiting_shelf", 409, "admin", event=True),
    # EF-EXP-053: a backfill over 100 Units or 5 GiB starts only once the estimate is confirmed
    ErrorCode("backfill.confirmation_required", 409, "user"),
    # EF-SRC-029: following from Shelfhold is refused while the service is paused, and leaving the
    # series out of the service's library asks for a confirmation
    ErrorCode("follow.service_paused", 409, "admin"),
    ErrorCode("follow.confirmation_required", 409, "user"),
    # EF-SRC-027: the series left the upstream library (not a purge: EF-BIB-053 is apart).
    ErrorCode("series.removed_upstream", 409, "admin", event=True),
)
