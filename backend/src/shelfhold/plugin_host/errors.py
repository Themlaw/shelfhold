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


class RefHasSecret(ContentRemoved):
    """A reference handed back by a source holds a secret or a `user:password@` part (EF-SRC-035).
    Nothing is stored. It is the Unit's problem, never the service's: a URL that merely looks like
    a credential must not stop every other series (review of L04), so the cycle counts it as a
    content error of that series and quarantines the Unit. The message names the field and a
    closed reason, never the value."""

    code = "source.ref_has_secret"


class AuthModeUnsupported(SourceError):
    """The tool runs in an authentication mode this release does not drive (EF-SRC-031)."""

    code = "source.auth_mode_unsupported"
    waiting_state = WaitingState.AUTH_MODE_UNSUPPORTED

    def __init__(self, message: str = AUTH_MODE_MESSAGE) -> None:
        super().__init__(message)


class ServiceInstanceChanged(SourceError):
    """The tool answers with another fingerprint: it was installed again (EF-SRC-036). The
    service pauses until `source reconnect`; nothing is deactivated, deleted or asked again."""

    code = "source.instance_changed"
    waiting_state = WaitingState.SOURCE_INSTANCE_CHANGED

    def __init__(self, message: str = "the connected service is another installation") -> None:
        super().__init__(message)


class UnsupportedAuthFlow(ValueError):
    """An AuthFlow reserved for a later milestone (V1 injects Basic and ApiKey only)."""


UNTRUSTED_CLASS = "untrusted"  # not a `source_failure` value: it is never counted


def failure_class_of(error: BaseException) -> str:
    """`content`, `auth`, `version`, `schema`, `instance` or `infra` (the values of
    `source_failure`), or `untrusted` for an archive the host refused (EF-BIB-051 (6)), which
    `source_failure` never sees."""
    if isinstance(error, UntrustedEntryError):
        return UNTRUSTED_CLASS
    if isinstance(error, ServiceVersionMismatch):
        return "version"
    if isinstance(error, ServiceInstanceChanged):
        return "instance"
    if isinstance(error, AuthRequired | AuthModeUnsupported):
        return "auth"
    if isinstance(error, SourceChanged):
        return "schema"
    if isinstance(error, ContentRemoved):
        return "content"
    return "infra"
