"""The tombstone screen the catalog is given (EF-BIB-049, EF-BIB-053, EF-BIB-054 (4)).

A service of its own, replayed over the normalizer versions in the code, so that the index it asks
is built under exactly those versions. It fails closed: with no library or no keyring there is no
screen, so there is no merge and no confirmation of an attach.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from shelfhold.catalog.public import NormalizerRegistry, default_registry
from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.library.public import CatalogScreenAdapter, normalizers_of
from shelfhold.ops.errors import ExitUnavailableError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["CatalogScreens"]


class CatalogScreens:
    def __init__(
        self,
        comp: Composition,
        registry: Callable[[], NormalizerRegistry] = default_registry,
    ) -> None:
        self._comp = comp
        self._registry = registry

    def open(self) -> CatalogScreenAdapter:
        """A replayed screen. Open it before any write transaction: the replay may write."""
        try:
            fsops, loaded, _cached = self._comp.read_context()
            service = self._comp.fresh_tombstones(fsops, loaded, normalizers_of(self._registry()))
        except (NoLibraryError, KeyringUnavailableError) as error:
            raise ExitUnavailableError(
                "the tombstones cannot be checked (no library or no keyring): nothing is merged"
            ) from error
        return CatalogScreenAdapter(service)
