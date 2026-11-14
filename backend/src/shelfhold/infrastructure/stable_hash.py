"""Hash a file before the write lock, look at it again under the lock, redo if it moved.

Reading a whole file takes as long as its disk (seconds for a CBZ on a NAS). Done while a write
transaction is open, it keeps every other writer waiting behind the hash, past their busy timeout.
So the hash is taken first, outside any transaction, together with what identifies the file
(inode, size, mtime). Under the lock only that identity is looked at again, which costs one
`stat`; a file that changed in between makes the transaction start over, with a new hash.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Protocol

from shelfhold.infrastructure.fsops.identity import FileIdentity

__all__ = ["FileChangedError", "HashedFiles", "retry_when_changed"]

ATTEMPTS: Final = 3


class FileChangedError(Exception):
    """A file was not the one hashed, when the lock was taken: the transaction starts over."""


class _Fsops(Protocol):
    def observe(self, local: str) -> tuple[str, FileIdentity] | None: ...

    def sha256_of(self, local: str) -> str: ...


class HashedFiles:
    """`hash(path)` outside the transaction, `verified(path)` inside it."""

    def __init__(self, fsops: _Fsops) -> None:
        self._fsops = fsops
        self._seen: dict[str, tuple[FileIdentity, str] | None] = {}
        self._unreadable: set[str] = set()

    def hash(self, local: str) -> tuple[FileIdentity, str] | None:
        """The identity and the digest of the file at `local`; `None` when it is not a file."""
        observed = self._fsops.observe(local)
        found = (
            (observed[1], self._fsops.sha256_of(local))
            if observed is not None and observed[0] == "file"
            else None
        )
        self._seen[local] = found
        return found

    def unreadable(self, local: str) -> None:
        """The file could not be looked at or read: `verified` says `None` for it, as for a file
        that is not there (the caller decides what that means)."""
        self._unreadable.add(local)

    def verified(self, local: str) -> tuple[FileIdentity, str] | None:
        """The identity and the digest taken before the lock, if the file is still the same (a
        `stat`, no read). `None`: it was not a file and still is not. Anything else is
        `FileChangedError`."""
        if local in self._unreadable:
            return None
        if local not in self._seen:
            raise FileChangedError(local)
        before = self._seen[local]
        now = self._fsops.observe(local)
        is_file = now is not None and now[0] == "file"
        if before is None:
            if is_file:
                raise FileChangedError(local)
            return None
        if not is_file or now is None or now[1] != before[0]:
            raise FileChangedError(local)
        return before


def retry_when_changed[T](work: Callable[[], T]) -> T:
    """`work` (hash, then transaction) again when a file moved between the two; the last
    attempt's `FileChangedError` goes up."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return work()
        except FileChangedError:
            if attempt == ATTEMPTS:
                raise
    raise AssertionError("unreachable")  # pragma: no cover
