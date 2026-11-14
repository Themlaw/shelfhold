"""The inbox of the follow cycle moves into the host area (`.received/` -> `.shelfhold/received/`).

Before 0.2.1 the archives the follow cycle receives sat in `<root>/.received/`: inside the library
folder, where Kavita could scan them and where nothing reserved the name. They now live under
`.shelfhold/`, which is reserved and excluded from Kavita. This runs at start-up, under the lease,
on the roots that are ready, and is repeatable: `Fsops` renames every regular file inside its own
root (confined, no replace) and the `replica` row of each file really moved follows it. What is
not moved (a name already taken, a sub-folder, a link, a legacy folder that is a link) is
reported and its row is left alone: it still names the file where it is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import coherence
from shelfhold.library.replica import paths_under, relocate_root_paths

__all__ = ["migrate_received_dirs"]

log = structlog.get_logger(__name__)


def migrate_received_dirs(
    engine: Engine,
    fsops: Fsops,
    roots: Sequence[tuple[str, str]],
    *,
    old_dir: str,
    new_dir: str,
) -> int:
    """Move what `old_dir` holds in each `(root_id, path)` root into `new_dir`; returns the
    number of files moved. A root that refuses (away, blocked, not writable) is reported and the
    others go on: the next start tries it again."""
    total = 0
    with engine.connect() as connection:  # EF-BIB-052: a root owing tombstone lines is not written
        owing = set(
            connection.execute(sa.select(coherence.tombstone_pending_root.c.root_id)).scalars()
        )
    for root_id, path in roots:
        if root_id in owing:
            continue
        try:
            moved, left = fsops.relocate_inbox(path, old_dir, new_dir)
        except OSError as error:
            log.warning("received.migration_skipped", root_id=root_id, error=type(error).__name__)
            continue
        names = set(moved) | _moved_before_a_crash(engine, fsops, root_id, path, old_dir, new_dir)
        if names:
            with write_transaction(engine) as connection:
                relocate_root_paths(
                    connection,
                    root_id,
                    [(f"{old_dir}/{n}", f"{new_dir}/{n}") for n in sorted(names)],
                )
        if left:
            log.warning("received.migration_left", root_id=root_id, count=len(left))
        total += len(moved)
    return total


def _moved_before_a_crash(
    engine: Engine, fsops: Fsops, root_id: str, path: str, old_dir: str, new_dir: str
) -> set[str]:
    """Files an interrupted run moved before it could write their row: the row still names the
    old place, where nothing is any more, and the file is in the new one."""
    with engine.connect() as connection:
        stale = paths_under(connection, root_id, old_dir)
    base = path.rstrip("/")
    healed: set[str] = set()
    for rel, size, file_hash in stale:
        name = rel[len(old_dir) + 1 :]
        if size is None and file_hash is None:
            continue  # nothing to check the file against: a row is never pointed at a guess
        new = f"{base}/{new_dir}/{name}"
        try:
            gone = fsops.observe(f"{base}/{rel}") is None
            seen = fsops.observe(new)
            same = (
                seen is not None
                and seen[0] == "file"
                and (size is None or seen[1].size == size)
                and (file_hash is None or fsops.sha256_of(new) == file_hash)
            )
        except OSError:  # a link, a root that refuses: nothing to heal there
            continue
        if gone and same:
            healed.add(name)
    return healed
