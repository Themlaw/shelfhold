"""Hash a file before the write lock, look at it again under the lock, redo if it moved.

Reading a whole file takes as long as its disk (seconds for a CBZ on a NAS). Done while a write
transaction is open, it keeps every other writer waiting behind the hash, past their busy timeout.
So the hash is taken first, outside any transaction, together with what identifies the file
(inode, size, mtime). Under the lock only that identity is looked at again, which costs one
`stat`; a file that changed in between makes the transaction start over, with a new hash.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from typing import Final, Protocol

from shelfhold.infrastructure.fsops.identity import FileIdentity

__all__ = ["FileChangedError", "HashedFiles", "retry_when_changed"]

ATTEMPTS: Final = 3
RACY_NS: Final = 50_000_000  # the timestamps of a file system tick much coarser than the clock
PAUSE_S: Final = (
    0.05  # before the second attempt; twice that before the third (plus a little jitter)
)


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
        self._ctime: dict[str, int | None] = {}
        self._unreadable: set[str] = set()

    def hash(self, local: str) -> tuple[FileIdentity, str] | None:
        """The identity and the digest of the file at `local`; `None` when it is not a file."""
        observed = self._fsops.observe(local)
        if observed is None or observed[0] != "file":
            self._seen[local] = self._ctime[local] = None
            return None
        before = self._changed_at(local)
        found = (observed[1], self._fsops.sha256_of(local))
        if self._changed_at(local) != before:
            raise FileChangedError(local)  # written while it was read: the digest is not its
        self._wait_out_the_tick(before)
        self._seen[local], self._ctime[local] = found, before
        return found

    @staticmethod
    def _wait_out_the_tick(changed_at: int | None) -> None:
        """A file touched a moment ago has the ctime of the tick it will be touched in again: a
        rewrite right after the hash would leave it as it is ("racily clean", as git calls it).
        Wait until it is old enough that a later rewrite shows in it. Files are rarely that young."""
        if changed_at is None:
            return
        age = time.time_ns() - changed_at
        if age < RACY_NS:
            time.sleep(min(RACY_NS - age, RACY_NS) / 1e9)

    def _changed_at(self, local: str) -> int | None:
        """The ctime of the file: what `utime` (`rsync -t`, `touch -r`) cannot put back, so a
        file rewritten in place with its mtime restored is seen (`Fsops.fingerprint`). A double
        that has no `fingerprint` is looked at on the disk."""
        fingerprint = getattr(self._fsops, "fingerprint", None)
        if fingerprint is not None:
            found = fingerprint(local)
            return None if found is None else int(found[4])
        try:
            return os.lstat(local).st_ctime_ns
        except OSError:
            return None

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
        if self._changed_at(local) != self._ctime[local]:
            raise FileChangedError(local)  # rewritten in place, its mtime put back
        return before


def retry_when_changed[T](work: Callable[[], T]) -> T:
    """`work` (hash, then transaction) again when a file moved between the two, after a short
    pause that grows (a copy that is about to end gets its chance). The last attempt's
    `FileChangedError` goes up: the caller turns it into a pending item, never into an error that
    leaves its plan half applied."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return work()
        except FileChangedError:
            if attempt == ATTEMPTS:
                raise
            time.sleep(PAUSE_S * attempt + random.uniform(0, PAUSE_S))  # noqa: S311
    raise AssertionError("unreachable")  # pragma: no cover
