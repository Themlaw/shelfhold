"""Which integrated backups still hold what a purge erased (EF-BIB-052 (5), read only).

A purge cannot reach a backup: the archive is sealed, and it is never opened, rewritten or erased
here. What it can do is say which archives were taken while the Unit (or the Work) existed, so that
the admin knows what to set aside by hand. The answer comes from the database alone (the runs that
ended well, `backup_run`) and from the names in the destination folder: an archive that was
pruned, or never written, is not listed, and nothing is decrypted, so no pass phrase is needed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.schema import platform
from shelfhold.ops.backup.store import BackupDirectory, DirectoryError

__all__ = ["RetainedBackup", "retained_since"]

_run = platform.backup_run


@dataclass(frozen=True)
class RetainedBackup:
    name: str  # `shelfhold-backup-<UTC stamp>.tar.age`: a time, never a title
    taken_at: datetime


def retained_since(engine: Engine, directory: Path, since: datetime) -> list[tuple[str, datetime]]:
    """The archives still in `directory` whose run ended at or after `since`, oldest first.

    `since` is when the purged rows were created: a backup taken before cannot hold them. A run
    that failed, or that is still running, wrote no archive worth listing."""
    try:
        present = set(BackupDirectory.open_existing(directory).names())
    except (DirectoryError, OSError):
        return []  # no destination to look at: nothing is listed, and nothing is invented
    floor = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(_run.c.file_name, _run.c.finished_at)
            .where(_run.c.state == "ok", _run.c.file_name.is_not(None),
                   _run.c.finished_at.is_not(None))
            .order_by(_run.c.finished_at, _run.c.id)
        ).all()  # fmt: skip
    found = []
    for name, finished in rows:
        when = finished if finished.tzinfo else finished.replace(tzinfo=UTC)
        if name in present and when >= floor:
            found.append((str(name), when))
    return found
