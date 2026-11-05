"""A tombstone gate for the benches that purge nothing: no content is ever refused."""

from types import SimpleNamespace
from typing import Any


class NoTombstones:
    def check(self, candidate: object, *, root_id: str | None = None, **_: Any) -> Any:
        return SimpleNamespace(blocked=False)

    def check_content(self, hashes: object = (), **_: Any) -> bool:
        return False


NONE_PURGED = NoTombstones()
