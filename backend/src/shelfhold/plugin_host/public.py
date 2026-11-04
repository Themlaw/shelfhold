"""Public facade of plugin_host (ENF-LEG-001).

A fresh install has no plugin repository and no active source; connected
services and metadata providers ship inactive. Wiring this to the real
settings (``config.py``, PR #2) is done once that PR is merged: the function
will then read ``plugin_repos`` (empty by default) from the loaded settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PluginState:
    plugin_repos: tuple[str, ...]
    active_sources: tuple[str, ...]
    active_meta_sources: tuple[str, ...]


def load_plugin_state(*, data_dir: Path, store_dir: Path) -> PluginState:
    """Plugin state of an instance rooted at ``data_dir`` / ``store_dir``."""
    del data_dir, store_dir  # nothing persisted yet: always the fresh-install state
    return PluginState(plugin_repos=(), active_sources=(), active_meta_sources=())
