"""Backups: the only writer of the backup directory (contract `backup-dir-sole-writer`).

The rest of the code reaches them through this facade; `store` is imported by `run` alone.
"""

from shelfhold.ops.backup.archive import BackupArchiveError
from shelfhold.ops.backup.jobs import BACKUP_KIND, register_backup_job
from shelfhold.ops.backup.restore import (
    DestinationError,
    PassphraseError,
    RestoreReport,
    restore_archive,
)
from shelfhold.ops.backup.run import (
    BACKUP_FAILURES,
    BackupError,
    BackupReport,
    BackupService,
    backup_warnings,
)
from shelfhold.ops.backup.store_missing import StoreOutcome, mark_store_missing

__all__ = [
    "BACKUP_FAILURES",
    "BACKUP_KIND",
    "BackupArchiveError",
    "BackupError",
    "BackupReport",
    "BackupService",
    "DestinationError",
    "PassphraseError",
    "RestoreReport",
    "StoreOutcome",
    "backup_warnings",
    "mark_store_missing",
    "register_backup_job",
    "restore_archive",
]
