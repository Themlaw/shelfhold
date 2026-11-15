# SPDX-License-Identifier: MIT
"""ComicVine, an optional native metadata provider (EF-BIB-013, ENF-LEG-005).

Off by default: a non-commercial source whose key is the instance's own. The Fetcher is handed to
it; it imports only the SDK.
"""

from shelfhold.plugin_host.adapters.native.comicvine.provider import (
    KEY,
    NOTICE,
    TERMS_URL,
    ComicVineProvider,
    build_manifest,
)

__all__ = ["KEY", "NOTICE", "TERMS_URL", "ComicVineProvider", "build_manifest"]
