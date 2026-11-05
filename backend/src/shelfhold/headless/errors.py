"""Headless configuration: refusals with stable catalog codes (EF-INT-024, EF-UX-003)."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("target.admin_risk_not_accepted", 409, "admin"),
    ErrorCode("target.exchange_failed", 502, "admin"),
    ErrorCode("target.conflict", 409, "admin"),
    ErrorCode("target.unknown", 404, "admin"),
    ErrorCode("config.unknown_key", 404, "admin"),
    ErrorCode("config.invalid_value", 422, "admin"),
    ErrorCode("config.key_readonly", 409, "admin"),
    ErrorCode("config.file_invalid", 422, "admin"),
    ErrorCode("source.conflict", 409, "admin"),
    ErrorCode("shelf.conflict", 409, "admin"),
    ErrorCode("source.unreachable", 502, "admin"),
)


class HeadlessError(Exception):
    """A refusal; `code` is the catalog code, the message never holds a secret."""

    code = "config.invalid_value"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class AdminRiskNotAccepted(HeadlessError):
    """The Kavita key is the key of an administrator account: whoever holds it can do anything in
    Kavita (ENF-SEC-013). The command stops on the warning until `--accept-admin-risk`."""

    code = "target.admin_risk_not_accepted"

    def __init__(self) -> None:
        super().__init__(
            "the Kavita key belongs to an administrator account and gives full control of Kavita;"
            " confirm with --accept-admin-risk"
        )


class TargetExchangeFailed(HeadlessError):
    code = "target.exchange_failed"


class TargetConflict(HeadlessError):
    code = "target.conflict"


class UnknownTarget(HeadlessError):
    code = "target.unknown"


class UnknownConfigKey(HeadlessError):
    code = "config.unknown_key"


class InvalidConfigValue(HeadlessError):
    code = "config.invalid_value"


class ReadOnlyConfigKey(HeadlessError):
    code = "config.key_readonly"


class ConfigFileError(HeadlessError):
    code = "config.file_invalid"


class SourceConflict(HeadlessError):
    code = "source.conflict"


class SourceAuthModeUnsupported(HeadlessError):
    """The server asks for a login this release cannot do (EF-SRC-031)."""

    code = "source.auth_mode_unsupported"


class ShelfConflict(HeadlessError):
    code = "shelf.conflict"


class SourceUnreachable(HeadlessError):
    code = "source.unreachable"
