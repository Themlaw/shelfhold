"""Typed failures of the plugin host and of connected services (EF-SRC-023, 031, 033).

The codes are those of the error catalog and of the operating events of the same identifier
(`acquisition/errors.py`, `events/operating.py`): nothing is declared twice here. `failure_class_of`
gives the class under which `source_failure` counts an error; only `content` counts toward the
disabling of a source (EF-BIB-042), so an authentication, version or schema problem pauses instead.
"""

from shelfhold.events.waiting_state import WaitingState
from shelfhold.infrastructure.content_trust import UntrustedEntryError
from shelfhold.sdk.source import (
    AuthRequired,
    ContentRemoved,
    SourceChanged,
    SourceError,
)

AUTH_MODE_MESSAGE = "mode multi-comptes indisponible dans cette version"


class AuthExpired(AuthRequired):
    """A session expired and `refresh` could not renew it: the service pauses (EF-SRC-023)."""

    code = "source.auth_expired"
    waiting_state = WaitingState.AUTH_EXPIRED


class ServiceVersionMismatch(SourceChanged):
    """The tool answers in a way this version of the adapter does not know: a version problem,
    never an authentication one (a CSRF 401 asks for no credentials)."""

    code = "source.version_unsupported"


class AuthModeUnsupported(SourceError):
    """The tool runs in an authentication mode this release does not drive (EF-SRC-031)."""

    code = "source.auth_mode_unsupported"
    waiting_state = WaitingState.AUTH_MODE_UNSUPPORTED

    def __init__(self, message: str = AUTH_MODE_MESSAGE) -> None:
        super().__init__(message)


class UnsupportedAuthFlow(ValueError):
    """An AuthFlow reserved for a later milestone (V1 injects Basic and ApiKey only)."""


UNTRUSTED_CLASS = "untrusted"  # not a `source_failure` value: it is never counted


def failure_class_of(error: BaseException) -> str:
    """`content`, `auth`, `version`, `schema` or `infra` (the values of `source_failure`), or
    `untrusted` for an archive the host refused (EF-BIB-051 (6)), which `source_failure` never
    sees."""
    if isinstance(error, UntrustedEntryError):
        return UNTRUSTED_CLASS
    if isinstance(error, ServiceVersionMismatch):
        return "version"
    if isinstance(error, AuthRequired | AuthModeUnsupported):
        return "auth"
    if isinstance(error, SourceChanged):
        return "schema"
    if isinstance(error, ContentRemoved):
        return "content"
    return "infra"
