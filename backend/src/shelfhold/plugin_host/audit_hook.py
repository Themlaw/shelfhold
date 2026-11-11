"""The plugin I/O detector in the host (EF-SRC-002): every refusal is logged with the plugin id.

The detector itself is `shelfhold.sdk.io_guard` (shared with the conformance kit). The host wraps
each loaded plugin with `audited`: while its code runs, a socket, a name lookup, a process, a
thread or a file outside its sandbox is refused before it happens (`ForbiddenIOError`, fail
closed) and logged as `plugin.forbidden_io`. It is a detector, not an isolation: see
docs/admin/plugins-tiers.md.
"""

import os
from typing import Any

import structlog

from shelfhold.infrastructure.logging import escaped
from shelfhold.sdk.io_guard import GuardedPlugin, IoViolation, allow_code_root, installed
from shelfhold.sdk.io_guard import install as _install

__all__ = ["allow_code_root", "audited", "install", "installed"]

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
    """`instance` with every method run under the detector, refusals logged."""
    install()
    return GuardedPlugin(instance, plugin_id, sandbox=sandbox, report=_report)
