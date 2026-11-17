# SPDX-License-Identifier: MIT
"""Hardcover, a native metadata provider asked by the server alone (EF-BIB-013, ENF-LEG-005).

The token is the instance's own, sealed by the host and handed to the provider as a value; a
browser never asks Hardcover. The Fetcher is handed to it; it imports only the SDK.
"""

from shelfhold.plugin_host.adapters.native.hardcover.provider import (
    KEY,
    HardcoverProvider,
    build_manifest,
)

__all__ = ["KEY", "HardcoverProvider", "build_manifest"]
