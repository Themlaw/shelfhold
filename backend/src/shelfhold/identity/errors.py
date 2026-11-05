"""Authentication and authorisation problems (EF-USR-021, EF-USR-022, EF-USR-024)."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("unauthenticated", 401, "user"),
    ErrorCode("forbidden", 403, "user"),
    ErrorCode("reauth_required", 401, "user"),
    ErrorCode("credential_not_allowed", 403, "user"),
    ErrorCode("invalid_credentials", 401, "user"),
    ErrorCode("too_many_attempts", 429, "user"),
    ErrorCode("csrf_failed", 403, "user"),
    ErrorCode("identity_header_refused", 401, "admin"),
)
