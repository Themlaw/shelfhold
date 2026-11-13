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
    ErrorCode("precondition_failed", 412, "user"),
    ErrorCode("precondition_required", 428, "user"),
    ErrorCode("idempotency_key_invalid", 400, "user"),
    ErrorCode("idempotency_key_reused", 422, "user"),
    ErrorCode("idempotency_in_progress", 409, "user"),
    ErrorCode("request_too_large", 413, "user"),
    ErrorCode("internal_error", 500, "admin"),
)
