"""Normalizer versions that a release ships, found the way plugins are (ENF-OPS-021, ENF-SEC-009).

A distribution declares them under the entry-point group `shelfhold.normalizers`. Each entry point
names a callable that returns `NormalizerDeclaration` objects (a new version of a normalizer that
exists). Like plugins, only a **first-party** distribution is ever imported: the distribution that
ships an entry point is read from its metadata before anything is loaded, so a stranger's code
never runs in M1a. A declaration the registry refuses, or an entry point that fails to load, is
logged and skipped; it never takes the host down, and the tombstone index refuses to build (it
fails closed) if a stored tombstone cites a version that did not load.
"""

import re
from collections.abc import Iterable
from importlib import metadata
from typing import Final

import structlog

from shelfhold.catalog.domain.normalizers import (
    NormalizerDeclaration,
    NormalizerRegistry,
    default_registry,
)

ENTRY_POINT_GROUP: Final = "shelfhold.normalizers"
FIRST_PARTY_DISTRIBUTIONS: Final = frozenset({"shelfhold"})  # as for plugins (plugin_host)

log = structlog.get_logger(__name__)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _first_party(entry_point: metadata.EntryPoint) -> bool:
    dist = entry_point.dist
    return dist is not None and _canonical(dist.name) in FIRST_PARTY_DISTRIBUTIONS


def load_registry(
    entry_points: Iterable[metadata.EntryPoint] | None = None,
) -> NormalizerRegistry:
    """The built-in versions, then the ones first-party distributions declare."""
    registry = default_registry()
    found = metadata.entry_points(group=ENTRY_POINT_GROUP) if entry_points is None else entry_points
    for entry_point in sorted(found, key=lambda e: e.name):
        if not _first_party(entry_point):
            log.warning("normalizer.third_party_ignored", entry_point=entry_point.name)
            continue
        try:
            declared = list(entry_point.load()())
            if not all(isinstance(d, NormalizerDeclaration) for d in declared):
                raise TypeError("not a NormalizerDeclaration")
        except Exception as error:
            log.warning("normalizer.load_failed", entry_point=entry_point.name,
                        error=type(error).__name__)  # fmt: skip
            continue
        for declaration, reason in registry.extend(declared):
            log.warning("normalizer.declaration_refused", name=declaration.name,
                        version=declaration.version, reason=reason)  # fmt: skip
    return registry
