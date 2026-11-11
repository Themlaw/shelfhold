# SPDX-License-Identifier: MIT
"""OpenLibrary, a native metadata provider (EF-BIB-013, ENF-LEG-005).

It imports only the SDK: the Fetcher is handed to it, and it never opens an image (the cover is a
reference the host downloads and decodes in the worker).
"""

from shelfhold.plugin_host.adapters.native.openlibrary.provider import (
    KEY,
    OpenLibraryProvider,
    build_manifest,
)

__all__ = ["KEY", "OpenLibraryProvider", "build_manifest"]
