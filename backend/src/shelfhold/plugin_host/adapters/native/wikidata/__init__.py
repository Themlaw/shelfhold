# SPDX-License-Identifier: MIT
"""The Wikidata provider: correspondences as suggestions only (EF-BIB-003, EF-BIB-013)."""

from shelfhold.plugin_host.adapters.native.wikidata.provider import (
    KEY,
    WikidataProvider,
    build_manifest,
    escape_sparql,
)

__all__ = ["KEY", "WikidataProvider", "build_manifest", "escape_sparql"]
