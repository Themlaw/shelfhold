"""Typed errors of a source, read as a state of the Health page (EF-SRC-014, EF-UX-003).

Each error the SDK or the host raises has one entry: its stable code (the one of the error
catalog and of the operating event), whether it concerns the whole service or one Unit, and the
way out. The way out is a waiting state when the error is one (`status` already knows its
command), else the bare command to run; this module names no command twice and holds no text.
The counting of failures is EF-BIB-042's, not redone here.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from shelfhold.events.waiting_state import WaitingState
from shelfhold.plugin_host import errors as host
from shelfhold.sdk import source as sdk

__all__ = [
    "BY_CODE",
    "NOT_A_SOURCE_STATE",
    "TYPED_ERRORS",
    "TypedState",
    "typed_for_code",
    "typed_state",
]

RECHECK: Final = "source recheck"  # look at the service again: the way out of most pauses


@dataclass(frozen=True)
class TypedState:
    code: str  # stable: the error catalog's
    scope: Literal["service", "unit"]  # the service as a whole, or one Unit (never a service state)
    waiting_state: str | None = None  # an EF-INT-023 state when the error is one
    command: str | None = None  # the bare command, when there is no waiting state


def _service(code: str, state: WaitingState | None = None, command: str = RECHECK) -> TypedState:
    return TypedState(code, "service", str(state) if state else None, None if state else command)


def _unit(code: str) -> TypedState:
    return TypedState(code, "unit", None, "status")  # `status` lists the Units concerned


TYPED_ERRORS: Final[Mapping[type[BaseException], TypedState]] = MappingProxyType(
    {
        sdk.SourceError: _service("source.error"),
        sdk.AuthRequired: _service("source.auth_required", WaitingState.AUTH_EXPIRED),
        host.AuthExpired: _service("source.auth_expired", WaitingState.AUTH_EXPIRED),
        sdk.RateLimited: _service("source.rate_limited"),
        sdk.TemporarilyUnavailable: _service("source.temporarily_unavailable"),
        sdk.SourceChanged: _service("source.changed"),
        host.ServiceVersionMismatch: _service("source.version_unsupported"),
        sdk.NotConfigured: _service("source.not_configured", command="plugin settings"),
        host.AuthModeUnsupported: _service(
            "source.auth_mode_unsupported", WaitingState.AUTH_MODE_UNSUPPORTED
        ),
        host.ServiceInstanceChanged: _service(
            "source.instance_changed", WaitingState.SOURCE_INSTANCE_CHANGED
        ),
        sdk.ContentRemoved: _unit("source.content_removed"),
        host.RefHasSecret: _unit("source.ref_has_secret"),
    }
)

# Raised for what the user typed, not for what a source is: no state of the page.
NOT_A_SOURCE_STATE: Final = frozenset({host.NoSourceForUrl})

# States that no exception carries: the follow cycle writes them (outage, source not loaded).
_RECORDED: Final = (_service("source.broken"), _service("source.not_loaded"))

BY_CODE: Final[Mapping[str, TypedState]] = MappingProxyType(
    {t.code: t for t in (*TYPED_ERRORS.values(), *_RECORDED)}
)


def typed_for_code(code: str) -> TypedState:
    """The state of a code a health document holds; an unknown code is the generic one."""
    return BY_CODE.get(code, TYPED_ERRORS[sdk.SourceError])


def typed_state(error: BaseException) -> TypedState:
    """The most specific entry of the error's class; any other error is the generic one."""
    for cls in type(error).__mro__:
        found = TYPED_ERRORS.get(cls)
        if found is not None:
            return found
    return TYPED_ERRORS[sdk.SourceError]
