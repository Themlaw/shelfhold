# SPDX-License-Identifier: MIT
"""Trapped EPUBs, built by code: the corpus the neutralizer is held against (EF-BIB-051 (8))."""

from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member, epub_bytes, xhtml, zip_bytes
from shelfhold.sdk.testing.corpus.trapped_epubs.cases import (
    CHAPTER,
    STYLESHEET,
    VECTOR_FAMILIES,
    Trap,
    all_traps,
    base_traps,
    mutation_traps,
)
from shelfhold.sdk.testing.corpus.trapped_epubs.detect import find_active_css, find_active_patterns

__all__ = [
    "CHAPTER",
    "STYLESHEET",
    "VECTOR_FAMILIES",
    "Member",
    "Trap",
    "all_traps",
    "base_traps",
    "epub_bytes",
    "find_active_css",
    "find_active_patterns",
    "mutation_traps",
    "xhtml",
    "zip_bytes",
]
