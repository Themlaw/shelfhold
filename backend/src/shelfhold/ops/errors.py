"""Refusals of the operating surface (EF-INT-023, EF-UX-003)."""

from shelfhold.error_catalog import ErrorCode

CODES: tuple[ErrorCode, ...] = (
    ErrorCode("ops.not_waiting", 409, "admin"),
    ErrorCode("ops.unknown_subject", 404, "admin"),
    ErrorCode("ops.exit_unavailable", 503, "admin"),
    ErrorCode("ops.invalid_choice", 422, "admin"),
    ErrorCode("ops.language_refused", 422, "admin"),
    ErrorCode("ops.still_waiting", 409, "admin"),
    ErrorCode("purge.collateral", 409, "admin"),
    ErrorCode("root.blocked", 409, "admin"),
)


class OpsError(Exception):
    """A refusal; `code` is the catalog code, the message never holds a secret."""

    code = "ops.not_waiting"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class NotWaitingError(OpsError):
    """The thing is not in the state this command leaves: nothing was changed."""

    code = "ops.not_waiting"


class UnknownSubjectError(OpsError):
    code = "ops.unknown_subject"


class ExitUnavailableError(OpsError):
    """The service behind this exit is not connected in this instance (its lot is not merged,
    or the instance has no such component)."""

    code = "ops.exit_unavailable"


class InvalidChoiceError(OpsError):
    code = "ops.invalid_choice"


class LanguageRefusedError(OpsError):
    code = "ops.language_refused"


class StillWaitingError(OpsError):
    """The command ran and the cause is still there (the server still answers 403...): the state
    stays, with its reason."""

    code = "ops.still_waiting"


class CodedRefusal(OpsError):
    """A refusal of another lot's service, under the catalog code that service already carries."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code
