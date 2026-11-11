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
    ErrorCode("source.dns_blocked", 502, "admin", event=True),
    ErrorCode("ssrf.blocked", 403, "admin"),
    ErrorCode("fetch.error", 502, "admin"),
    ErrorCode("fetch.dns_failed", 502, "admin"),
    ErrorCode("fetch.invalid_url", 422, "user"),
    ErrorCode("fetch.too_many_redirects", 502, "admin"),
    ErrorCode("fetch.unsafe_redirect", 502, "admin"),
    ErrorCode("fetch.too_large", 502, "admin"),
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
