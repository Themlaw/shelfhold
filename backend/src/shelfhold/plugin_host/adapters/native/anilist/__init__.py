# SPDX-License-Identifier: MIT
"""AniList, a native metadata provider used for lookups only (EF-BIB-013, ENF-LEG-005).

It imports only the SDK: the Fetcher is handed to it. No account of anyone is read or written.
"""

from shelfhold.plugin_host.adapters.native.anilist.provider import (
    KEY,
    AniListProvider,
    build_manifest,
)

__all__ = ["KEY", "AniListProvider", "build_manifest"]
