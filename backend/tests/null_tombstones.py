"""A tombstone gate for the benches that purge nothing: no content is ever refused."""

from types import SimpleNamespace
from typing import Any


class NoTombstones:
    def check(self, candidate: object, *, root_id: str | None = None, **_: Any) -> Any:
        return SimpleNamespace(blocked=False)

    def check_content(self, hashes: object = (), **_: Any) -> bool:
        return False


NONE_PURGED = NoTombstones()


class NoCatalogHits:
    """The catalog's screen for the benches that purged nothing: no place, no Work identifier is
    tombstoned."""

    def places(self, places: object) -> set[str]:
        return set()

    def work_ids(self, ids: object) -> set[tuple[str, str]]:
        return set()


NO_CATALOG_HITS = NoCatalogHits()
