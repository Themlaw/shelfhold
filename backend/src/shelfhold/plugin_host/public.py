"""Public facade of plugin_host (ENF-LEG-001).

A fresh install has no plugin repository and no active source; connected
services and metadata providers ship inactive. The values come from the real
settings (``plugin_repos`` and ``active_sources``, both empty by default).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

import structlog

from shelfhold.config import Settings
from shelfhold.plugin_host.install import Layout, read_state, verdicts
from shelfhold.plugin_host.registry import (
    InstalledPlugin,
    PluginCandidate,
    PluginOrigin,
    PluginRecord,
    PluginRegistry,
    PluginStatus,
    third_party_candidates,
)

__all__ = [
    "PluginCandidate",
    "PluginOrigin",
    "PluginRecord",
    "PluginRegistry",
    "PluginState",
    "PluginStatus",
    "load_plugin_state",
    "start_plugins",
]


@dataclass(frozen=True)
class PluginState:
    plugin_repos: tuple[str, ...]
    active_sources: tuple[str, ...]
    active_meta_sources: tuple[str, ...]


def load_plugin_state(settings: Settings) -> PluginState:
    """Plugin state derived from the resolved settings."""
    # No metadata provider is active unless it is explicitly listed as an active source.
    return PluginState(
        plugin_repos=settings.plugin_repos,
        active_sources=settings.active_sources,
        active_meta_sources=(),
    )


def start_plugins(
    settings: Settings, environ: Mapping[str, str] | None = None
) -> tuple[PluginRegistry, list[PluginRecord]]:
    """The one place where third-party code can be loaded, once, at start-up (EF-SRC-019).

    A plugin loads only with ``SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1``, its typed confirmation and
    unchanged files; otherwise it is reported as disabled with the reason and never imported.
    The loader is handed nothing: no settings, no secret, no host object.
    """
    layout = Layout.of(settings.data_dir)
    state = read_state(layout)
    judged = verdicts(layout, os.environ if environ is None else dict(environ))
    blocked = {v.name: v.reason or "refused" for v in judged if not v.granted}
    granted = [v.name for v in judged if v.granted]
    installed = [InstalledPlugin(i.name, i.top_levels) for i in state.installed.values()]
    plugin_state = load_plugin_state(settings)
    registry = PluginRegistry(
        active_keys=plugin_state.active_sources, granted_distributions=granted
    )
    records = registry.load_all(third_party_candidates(layout.site, installed, blocked=blocked))
    log = structlog.get_logger()
    for record in records:
        log.info(
            "plugin.third_party", key=record.key, status=record.status.value, reason=record.reason
        )
    return registry, records
