"""Authentication and authorisation problems (EF-USR-021, EF-USR-022, EF-USR-024)."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("unauthenticated", 401, "user"),
    ErrorCode("forbidden", 403, "user"),
    ErrorCode("reauth_required", 401, "user"),
    ErrorCode("credential_not_allowed", 403, "user"),
    ErrorCode("invalid_credentials", 401, "user"),
    ErrorCode("device_tls_required", 403, "admin"),
    # M4 L46b: another profile's token answers like one that is not there (no probing)
    ErrorCode("device_token_not_found", 404, "user"),  # M4 L46: an e-reader over plain HTTP
    # M5 L02: two rotations of one device raced; the one that lost shows no secret
    ErrorCode("device_rotation_conflict", 409, "user"),
    ErrorCode("remember_needs_confirmation", 422, "user"),
    ErrorCode("too_many_attempts", 429, "user"),
    ErrorCode("password_login_suspended", 409, "admin"),  # a restore: passwords may be old ones
    ErrorCode("csrf_failed", 403, "user"),
    # The first account made in the wizard (M3 ENF-UX-001): behind the secret of the compose
    ErrorCode("setup_token_invalid", 403, "user"),
    ErrorCode("setup_token_not_configured", 409, "admin"),
    ErrorCode("setup_closed", 409, "user"),
    # Passkeys (EF-USR-025, ADR-0050)
    ErrorCode("passkey_unavailable", 409, "admin"),
    ErrorCode("passkey_refused", 422, "user"),
    ErrorCode("passkey_not_found", 404, "user"),
    ErrorCode("last_login_factor", 409, "user"),
    # OpenID Connect (M5 L03): the provider cannot be asked; an admin's link the rules refuse (the
    # rule is in the `reason` extension, never a `sub`); a child profile is never linked
    ErrorCode("oidc_unavailable", 503, "admin"),
    ErrorCode("oidc_conflict", 409, "user"),
    ErrorCode("oidc_child_profile", 403, "user"),
    # an admin links her own account or an adult member's, never another admin's; a link that is
    # no longer waiting for its confirmation (answered, expired, or another session's)
    ErrorCode("oidc_admin_target", 403, "user"),
    ErrorCode("oidc_link_expired", 409, "user"),
    ErrorCode("oidc_not_linked", 404, "user"),  # M5 L04: nothing to unlink, or to prove again
    # Sessions (M3 L07): another profile's session answers like one that is not there (no probing)
    ErrorCode("session_not_found", 404, "user"),
    # SEC-1: a read-only key asked with a scope that writes (`admin`, `request`): refused, never
    # made with a scope that would quietly write
    ErrorCode("key_scope_read_only_conflict", 422, "admin"),
    # Household profiles (M3 L05): the rule that refused is in the `reason` extension
    ErrorCode("profile_not_found", 404, "user"),
    ErrorCode("profile_conflict", 409, "user"),
    ErrorCode("profile_refused", 422, "user"),
    # Per-user state (M3 L36): a claim from the future, a language tag that is not one
    ErrorCode("user_state_refused", 422, "user"),
    # A member's Kavita account (M4 L12): another's answers like one that is not there; the rule
    # that refused is in the `reason` extension (never the key, never a name)
    ErrorCode("destination_account_not_found", 404, "user"),
    ErrorCode("destination_account_conflict", 409, "user"),
    ErrorCode("destination_account_refused", 422, "user"),
    ErrorCode("destination_account_child_refused", 422, "user"),
    # The Apprise destination of one adult profile (M4 L39): a URL the allowlist refuses
    ErrorCode("notify_destination_refused", 422, "user"),
    # A profile's tracker link (M6 L13, EF-INT-010): another's answers like one that is not
    # there; a child without the admin's allowance; a push without the profile's accord; the
    # remote service's refusal. The rule that refused is in the `reason` extension, never a token
    ErrorCode("tracker_not_found", 404, "user"),
    ErrorCode("tracker_child_refused", 403, "user"),
    ErrorCode("tracker_push_refused", 403, "user"),
    ErrorCode("tracker_refused", 422, "user"),
    # Notification preferences (M3 L14): a mode, quiet hours or a time zone that is not one
    ErrorCode("notification_prefs_refused", 422, "user"),
)
