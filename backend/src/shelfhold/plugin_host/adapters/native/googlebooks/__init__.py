# SPDX-License-Identifier: MIT
"""Google Books, a native metadata provider with the key of the instance (EF-BIB-013, ENF-LEG-005).

A daily budget (1000 by default, adjustable), counted per Pacific day by the host, and a 429
«Queries per day» that suspends it until Pacific midnight. The Fetcher is handed to it; it imports
only the SDK.
"""

from shelfhold.plugin_host.adapters.native.googlebooks.provider import (
    DEFAULT_DAILY_BUDGET,
    KEY,
    PACIFIC,
    GoogleBooksProvider,
    build_manifest,
)

__all__ = ["DEFAULT_DAILY_BUDGET", "KEY", "PACIFIC", "GoogleBooksProvider", "build_manifest"]
