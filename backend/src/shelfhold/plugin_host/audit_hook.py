"""The plugin I/O detector in the host (EF-SRC-002): every refusal is logged with the plugin id.

The detector itself is `shelfhold.sdk.io_guard` (shared with the conformance kit, which runs it in
`raise` mode). The host runs it in `report` mode: each loaded plugin is wrapped with `audited`, and
its import goes through `importing`; while its code runs, a socket, a name lookup, a process, a
thread start or a file outside its sandbox is logged as `plugin.forbidden_io` with the plugin id,
and nothing is blocked. It is a detector, not the trust boundary (that is the M6 sandbox): see
docs/admin/plugins-tiers.md.
"""

import contextlib
import os
from collections.abc import Iterator
from typing import Any

import structlog

from shelfhold.infrastructure.logging import escaped
from shelfhold.sdk.io_guard import (
    GuardedPlugin,
    IoViolation,
    allow_code_root,
    host_io,
    installed,
    plugin_io,
)
from shelfhold.sdk.io_guard import install as _install

__all__ = ["allow_code_root", "audited", "host_io", "importing", "install", "installed"]

log = structlog.get_logger(__name__)


def install() -> None:
    """Add the process-wide audit hook (idempotent)."""
    _install()


def _report(violation: IoViolation) -> None:
    log.warning(
        "plugin.forbidden_io",
        plugin_id=violation.plugin_id,
        io_event=violation.event,
        detail=escaped(violation.detail, limit=300),
    )


def audited(
    instance: Any, plugin_id: str, *, sandbox: str | os.PathLike[str] | None = None
) -> GuardedPlugin:
    """`instance` with every method run under the detector, in `report` mode: logged, never
    blocked."""
    install()
    return GuardedPlugin(instance, plugin_id, sandbox=sandbox, report=_report, mode="report")


@contextlib.contextmanager
def importing(plugin_id: str) -> Iterator[None]:
    """The import of a plugin's code, under the detector in `report` mode."""
    with plugin_io(plugin_id, report=_report, mode="report"):
        yield
