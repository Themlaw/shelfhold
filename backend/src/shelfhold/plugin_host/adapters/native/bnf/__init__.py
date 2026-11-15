# SPDX-License-Identifier: MIT
"""The BnF provider: SRU + SPARQL, Licence Ouverte (EF-BIB-013, ENF-LEG-005)."""

from shelfhold.plugin_host.adapters.native.bnf.provider import (
    KEY,
    MAX_QUERY,
    BnFProvider,
    SruReader,
    build_manifest,
    escape_sparql,
    escape_sru,
)

__all__ = [
    "KEY",
    "MAX_QUERY",
    "BnFProvider",
    "SruReader",
    "build_manifest",
    "escape_sparql",
    "escape_sru",
]
