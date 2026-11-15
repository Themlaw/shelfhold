# SPDX-License-Identifier: MIT
"""MangaUpdates, a native metadata provider with a release calendar (EF-BIB-013, ENF-LEG-005).

It imports only the SDK: the Fetcher is handed to it.
"""

from shelfhold.plugin_host.adapters.native.mangaupdates.provider import (
    ATTRIBUTION,
    KEY,
    MangaUpdatesProvider,
    build_manifest,
)

__all__ = ["ATTRIBUTION", "KEY", "MangaUpdatesProvider", "build_manifest"]
