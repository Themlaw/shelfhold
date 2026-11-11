"""EPUB packaging: the only writer of an OPF (contract `opf-sole-writer`, EF-EXP-033).

A passthrough: the EPUB is the one the library holds (for an `untrusted` source, the derivative
the neutralizer made, never the original), its OPF gets the few elements the specification lists,
and every other entry is copied byte for byte. No age is ever written (D1): the edit has no field
that could carry one.
"""

from shelfhold.infrastructure.packaging.epub.opf import (
    OpfEdit,
    OpfError,
    OpfRewrite,
    find_opf_path,
    rewrite_opf,
)
from shelfhold.infrastructure.packaging.epub.writer import RepackagedEpub, repackage_epub

__all__ = [
    "OpfEdit",
    "OpfError",
    "OpfRewrite",
    "RepackagedEpub",
    "find_opf_path",
    "repackage_epub",
    "rewrite_opf",
]
