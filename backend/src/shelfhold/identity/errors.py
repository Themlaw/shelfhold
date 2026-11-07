"""Authentication and authorisation problems (EF-USR-021, EF-USR-022, EF-USR-024)."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("unauthenticated", 401, "user"),
    ErrorCode("forbidden", 403, "user"),
    ErrorCode("reauth_required", 401, "user"),
    ErrorCode("credential_not_allowed", 403, "user"),
    ErrorCode("invalid_credentials", 401, "user"),
    ErrorCode("remember_needs_confirmation", 422, "user"),
    ErrorCode("too_many_attempts", 429, "user"),
    ErrorCode("csrf_failed", 403, "user"),
    # Passkeys (EF-USR-025, ADR-0050)
    ErrorCode("passkey_unavailable", 409, "admin"),
    ErrorCode("passkey_refused", 422, "user"),
    ErrorCode("passkey_not_found", 404, "user"),
    ErrorCode("last_login_factor", 409, "user"),
)
