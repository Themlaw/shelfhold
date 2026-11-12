"""The independence of a kept copy is read on the disk, never in the stored rows (EF-BIB-027,
EF-EXP-015): a restore that made one name a link to the other leaves every `replica` row with the
old inode and `independent = true`. What decides is the `(st_dev, st_ino)` and the `nlink` of the
files as they are when the decision is taken."""

from contextlib import suppress
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import storage
from shelfhold.library.replica import library_copies, record_library_rewrite


class Disk(Protocol):
    """What `Fsops` answers about a file (the library does not import fsops: ENF contract)."""

    def link_stat(self, local: str) -> tuple[int, int, int, int, int]: ...
    def hash_and_identity(self, local: str) -> tuple[str, Any, int] | None: ...
    def observe(self, local: str, *, host: bool = False) -> Any: ...


__all__ = ["fresh_library_inodes", "is_own_inode", "refresh_library_rows"]


def fresh_library_inodes(connection: Connection, fsops: Disk, unit_id: str) -> set[tuple[int, int]]:
    """`(dev, ino)` of the Unit's library files as they are now (a missing file is no sibling)."""
    found: set[tuple[int, int]] = set()
    for copy in library_copies(connection, unit_id):
        with suppress(OSError):
            dev, ino, *_ = fsops.link_stat(copy.local)
            found.add((dev, ino))
    return found


def is_own_inode(
    fsops: Disk, local: str, siblings: set[tuple[int, int]]
) -> tuple[bool, tuple[int, int, int] | None]:
    """Is `local` an inode of its own, now: one name, and not the inode of any sibling? Also its
    `(dev, ino, nlink)`, None when it cannot be read."""
    try:
        dev, ino, nlink, *_ = fsops.link_stat(local)
    except OSError:
        return False, None
    return nlink == 1 and (dev, ino) not in siblings, (dev, ino, nlink)


def refresh_library_rows(connection: Connection, fsops: Disk, unit_id: str, now: datetime) -> None:
    """The library rows say what the disk says: a row whose inode is no longer the file's is
    rewritten, but only when the bytes are still the ones the row names (a changed file is the
    integrity check's business, never hidden here)."""
    table = storage.replica
    rows = connection.execute(
        sa.select(table.c.id, table.c.dev, table.c.ino, table.c.file_hash).where(
            table.c.unit_id == unit_id, table.c.location == "library"
        )
    ).all()
    by_id = {c.replica_id: c for c in library_copies(connection, unit_id)}
    for row in rows:
        copy = by_id.get(row.id)
        if copy is None:
            continue
        with suppress(OSError):
            dev, ino, _nlink, mtime_ns, _ctime = fsops.link_stat(copy.local)
            if (dev, ino) == (row.dev, row.ino):
                continue
            seen = fsops.hash_and_identity(copy.local)
            if seen is None or seen[0] != row.file_hash:
                continue
            record_library_rewrite(
                connection, row.id, file_hash=row.file_hash, dev=dev, ino=ino, size=seen[1].size,
                mtime_ns=mtime_ns, verified_at=now,
            )  # fmt: skip
