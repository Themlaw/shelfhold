"""Out-of-the-box plugin and source state (ENF-LEG-001).

A fresh install has no plugin repository configured and no active source;
connected services (including metadata providers) ship inactive.
"""

from __future__ import annotations


def default_plugin_repos() -> tuple[str, ...]:
    return ()


def default_meta_sources_active() -> tuple[str, ...]:
    return ()


def default_active_sources() -> tuple[str, ...]:
    return default_meta_sources_active()
