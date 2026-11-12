"""Typed errors of the SDK and of the plugin host, mapped to a state and an action (EF-SRC-014).

Every error a source can raise says which code it carries, whether it concerns the service or one
Unit, and which command the administrator runs: a command that exists, checked against the parser
of the CLI and the table of the waiting states."""

import pytest

from shelfhold.i18n import load_catalog
from shelfhold.ops.health import action_of
from shelfhold.plugin_host import errors as host_errors
from shelfhold.plugin_host import health
from shelfhold.sdk import source as sdk
from tests.health_env import cli_exists, cli_words


def _all_source_errors() -> set[type[sdk.SourceError]]:
    found: set[type[sdk.SourceError]] = set()
    pending: list[type[sdk.SourceError]] = [sdk.SourceError]
    while pending:
        for child in pending.pop().__subclasses__():
            found.add(child)
            pending.append(child)
    return found


@pytest.mark.req("EF-SRC-014")
def test_source_health_typed_errors_mapped_to_ui_action() -> None:
    assert host_errors.AuthExpired  # imported: its subclasses are registered
    classes = _all_source_errors() | {sdk.SourceError}
    unmapped = {c.__name__ for c in classes if c not in health.TYPED_ERRORS} - {
        c.__name__ for c in health.NOT_A_SOURCE_STATE
    }
    assert not unmapped, f"typed errors without a state and an action: {sorted(unmapped)}"
    for cls in sorted(health.TYPED_ERRORS, key=lambda c: c.__name__):
        typed = health.TYPED_ERRORS[cls]
        assert typed.code == getattr(cls, "code", "source.error"), cls  # the stable code
        assert typed.scope in {"service", "unit"}
        action = action_of(typed)
        assert cli_exists(action.command), f"{cls.__name__}: `{action.command}` is not a command"
        assert cli_words(action.command), cls
        catalog = load_catalog()
        assert f"errors.{typed.code}.action" in catalog, f"{typed.code}: no text for the action"


@pytest.mark.req("EF-SRC-014")
def test_typed_state_reads_an_instance_by_its_class() -> None:
    assert health.typed_state(host_errors.AuthExpired("x")).waiting_state == "auth_expired"
    assert health.typed_state(host_errors.ServiceInstanceChanged()).code == "source.instance_changed"
    assert health.typed_state(sdk.ContentRemoved("gone")).scope == "unit"
    # an error nobody typed is the generic one: still a code and an action, never a stack trace
    generic = health.typed_state(RuntimeError("boom"))
    assert generic.code == "source.error"
    assert cli_exists(action_of(generic).command)


@pytest.mark.req("EF-SRC-014")
def test_action_of_reuses_the_status_commands() -> None:
    """A waiting state leaves by the command `status` already gives: not a second table."""
    from shelfhold.ops.status import command_for

    typed = health.typed_state(host_errors.AuthExpired("x"))
    command, reserved = command_for("auth_expired", "")
    assert action_of(typed).command == command
    assert action_of(typed).reserved is reserved
    assert action_of(health.typed_state(host_errors.ServiceInstanceChanged())).command == (
        command_for("source_instance_changed", "")[0]
    )
