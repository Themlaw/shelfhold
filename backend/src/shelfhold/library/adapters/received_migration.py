"""The inbox of the follow cycle moves into the host area (`.received/` -> `.shelfhold/received/`).

Before 0.2.1 the archives the follow cycle receives sat in `<root>/.received/`: inside the library
folder, where Kavita could scan them and where nothing reserved the name. They now live under
`.shelfhold/`, which is reserved and excluded from Kavita. This runs at start-up, under the lease,
and is repeatable: `Fsops` renames every file inside its own root (confined, no replace), the
`replica` rows follow, and an interrupted run is finished by the next one.
"""

from collections.abc import Sequence

from sqlalchemy import Engine

from shelfhold.infrastructure.fsops import Fsops
from shelfhold.library.replica import relocate_root_paths

__all__ = ["migrate_received_dirs"]


def migrate_received_dirs(
    engine: Engine,
    fsops: Fsops,
    roots: Sequence[tuple[str, str]],
    *,
    old_dir: str,
    new_dir: str,
) -> int:
    """Move what `old_dir` holds in each `(root_id, path)` root into `new_dir`; returns the
    number of files moved. A name already taken in `new_dir` stays where it is."""
    moved = 0
    for root_id, path in roots:
        moved += fsops.relocate_inbox(path, old_dir, new_dir)
        with engine.begin() as connection:  # every start: an interrupted run heals
            relocate_root_paths(connection, root_id, old_dir, new_dir)
    return moved
