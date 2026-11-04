"""Public facade of plugin_host (ENF-LEG-001).

A fresh install has no plugin repository and no active source; connected
services and metadata providers ship inactive. The values come from the real
settings (``plugin_repos`` and ``active_sources``, both empty by default).
"""

from __future__ import annotations

from dataclasses import dataclass

from shelfhold.config import Settings


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
