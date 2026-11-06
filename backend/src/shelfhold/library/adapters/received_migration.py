"""The inbox of the follow cycle moves into the host area (`.received/` -> `.shelfhold/received/`).

Before 0.2.1 the archives the follow cycle receives sat in `<root>/.received/`: inside the library
folder, where Kavita could scan them and where nothing reserved the name. They now live under
`.shelfhold/`, which is reserved and excluded from Kavita. This runs at start-up, under the lease,
and is repeatable: every file is renamed inside its own root (a confined, no-replace rename), the
`replica` rows follow, and an interrupted run is finished by the next one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import storage

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
    number of files moved. A name already taken in `new_dir` stays where it is (never replaced)."""
    moved = 0
    for root_id, path in roots:
        old = f"{path.rstrip('/')}/{old_dir}"
        new = f"{path.rstrip('/')}/{new_dir}"
        found = fsops.observe(old)
        if found is not None and found[0] == "dir":
            fsops.mkdir(new)
            for name, kind in sorted(fsops.list_dir(old).items()):
                if kind != "file":
                    continue
                try:
                    fsops.rename(f"{old}/{name}", f"{new}/{name}")
                except FileExistsError:
                    log.warning("received.migration_conflict", root_id=root_id)
                    continue
                moved += 1
            if not fsops.list_dir(old):
                fsops.rmdir(old)
        _follow_rows(engine, root_id, old_dir, new_dir)  # every start: an interrupted run heals
    return moved


def _follow_rows(engine: Engine, root_id: str, old_dir: str, new_dir: str) -> None:
    replica = storage.replica
    prefix = f"{old_dir}/"
    with engine.begin() as connection:
        connection.execute(
            sa.update(replica)
            .where(
                replica.c.physical_root_id == root_id,
                replica.c.rel_path.like(f"{prefix}%"),
            )
            .values(
                rel_path=sa.literal(f"{new_dir}/", sa.Text).concat(
                    sa.func.substr(replica.c.rel_path, len(prefix) + 1)
                )
            )
        )
