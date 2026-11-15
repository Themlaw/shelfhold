"""Protocol-level problems of the REST API (RFC 9457, EF-INT-001)."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("bad_request", 400, "user"),
    ErrorCode("validation_failed", 422, "user"),
    ErrorCode("not_found", 404, "user"),
    ErrorCode("method_not_allowed", 405, "user"),
    ErrorCode("invalid_cursor", 400, "user"),
    ErrorCode("resync_required", 410, "user"),
    ErrorCode("too_many_streams", 429, "user"),
    ErrorCode("bundle_gone", 410, "user"),  # M4 L49a: a block that cannot be served any more
    ErrorCode("bundle_busy", 429, "user"),  # M4 L49a: too many blocks at a time
    ErrorCode("kosync_registration_disabled", 402, "user"),  # M4 L47: accounts are Shelfhold's
    ErrorCode("kosync_ambiguous_document", 409, "user"),  # M4 L47: one digest, two visible files
    ErrorCode("precondition_failed", 412, "user"),
    ErrorCode("precondition_required", 428, "user"),
    ErrorCode("idempotency_key_invalid", 400, "user"),
    ErrorCode("idempotency_key_reused", 422, "user"),
    ErrorCode("idempotency_in_progress", 409, "user"),
    ErrorCode("request_too_large", 413, "user"),
    ErrorCode("internal_error", 500, "admin"),
)
