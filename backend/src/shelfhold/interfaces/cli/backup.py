"""``shelfhold backup run|verify|restore`` (EF-INT-014, EF-INT-015).

`run` works from the host with or without the server: a separate process that holds the
maintenance gate through its lock file, so a `VACUUM` of the server and a backup exclude one
another. The passphrase comes from the file named by `SHELFHOLD_BACKUP_PASSPHRASE_FILE`, never
from the command line. `verify` and `restore` need no database: they run on a host where only the
backups survived. A damaged, truncated or altered archive, and a wrong passphrase, all print the
same sentence.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.infrastructure.secrets.errors import ExportError
from shelfhold.infrastructure.secrets.export import read_passphrase_file
from shelfhold.integrations.apprise import operating_bus
from shelfhold.jobs.maintenance import MaintenanceGate
from shelfhold.ops.backup import (
    BackupArchiveError,
    BackupError,
    BackupService,
    DestinationError,
    PassphraseError,
    restore_archive,
)
from shelfhold.ops.backup.run import verify_latest
from shelfhold.ops.backup.verify import check_archive, read_archive_file

GATE_FILE = ".maintenance.lock"

# One literal `t()` per sentence (the catalog test reads them): a cause maps to its sentence.
FAILED_TEXT: dict[str, Callable[[], str]] = {
    "passphrase_missing": lambda: t("backup.failed_passphrase_missing"),
    "passphrase_invalid": lambda: t("backup.failed_passphrase_invalid"),
    "passphrase_colocated": lambda: t("backup.failed_passphrase_colocated"),
    "destination_missing": lambda: t("backup.failed_destination_missing"),
    "destination_unavailable": lambda: t("backup.failed_destination_unavailable"),
    "destination_not_distinct": lambda: t("backup.failed_destination_not_distinct"),
    "no_room": lambda: t("backup.failed_no_room"),
    "too_large": lambda: t("backup.failed_too_large"),
    "busy": lambda: t("backup.failed_busy"),
    "keyring_unavailable": lambda: t("backup.failed_keyring_unavailable"),
    "database_error": lambda: t("backup.failed_database_error"),
    "write_failed": lambda: t("backup.failed_write_failed"),
    "verify_failed": lambda: t("backup.failed_verify_failed"),
}
DESTINATION_TEXT: dict[str, Callable[[str], str]] = {
    "not_empty": lambda path: t("backup.destination_not_empty", path=path),
    "is_live_data": lambda path: t("backup.destination_is_live_data", path=path),
    "unusable": lambda path: t("backup.destination_unusable", path=path),
}


def failed_text(reason: str) -> str:
    return FAILED_TEXT[reason]()


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("backup", help=t("backup.help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="backup_command", required=True)

    run = actions.add_parser("run", help=t("backup.run_help"), allow_abbrev=False)
    run.add_argument(
        "--accept-colocated-passphrase", action="store_true", help=t("backup.colocated_help")
    )
    run.add_argument("--with-manifests", action="store_true", help=t("backup.manifests_help"))
    run.set_defaults(handler=run_backup, configure_logging=False)

    verify = actions.add_parser("verify", help=t("backup.verify_help"), allow_abbrev=False)
    verify.add_argument("--file", type=Path, help=t("backup.file_help"))
    verify.add_argument("--passphrase-file", type=Path, help=t("backup.passphrase_file_help"))
    verify.set_defaults(handler=run_verify, configure_logging=False)

    restore = actions.add_parser("restore", help=t("backup.restore_help"), allow_abbrev=False)
    restore.add_argument("--from", required=True, dest="archive", type=Path,
                         help=t("backup.from_help"))  # fmt: skip
    restore.add_argument("--to", required=True, dest="destination", type=Path,
                         help=t("backup.to_help"))  # fmt: skip
    restore.add_argument("--passphrase-file", type=Path, help=t("backup.passphrase_file_help"))
    restore.set_defaults(handler=run_restore, configure_logging=False)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def run_backup(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    settings = resolved.values
    database = settings.database_path
    if not database.exists() or current_revision(database) != head_revision():
        return _fail(t("backup.no_database"))
    engine = create_db_engine(database)
    clock = SystemClock()
    try:
        service = BackupService(
            settings, engine, clock=clock, bus=operating_bus(clock),
            gate=MaintenanceGate(settings.data_dir / GATE_FILE),
        )  # fmt: skip
        report = service.run(
            accept_colocated=args.accept_colocated_passphrase, manifests=args.with_manifests
        )
    except BackupError as error:
        return _fail(failed_text(error.reason))
    finally:
        engine.dispose()
    print(t("backup.done", name=report.name, size=report.size, members=report.members))
    for name in report.pruned:
        print(t("backup.pruned", name=name))
    return 0


def run_verify(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    settings = resolved.values
    try:
        if args.file is not None:
            path = args.passphrase_file or settings.backup_passphrase_file
            if path is None:
                return _fail(t("backup.failed_passphrase_missing"))
            try:
                passphrase = read_passphrase_file(path)
            except ExportError:
                return _fail(t("backup.failed_passphrase_invalid"))
            name, checked = str(args.file), check_archive(read_archive_file(args.file), passphrase)
        else:
            name, checked = verify_latest(settings, passphrase_file=args.passphrase_file)
    except BackupError as error:
        return _fail(failed_text(error.reason))
    except BackupArchiveError as error:
        return _fail(t("backup.damaged", detail=error))
    created, revision = checked.created_at, checked.schema_revision
    print(t("backup.verified", name=name, members=checked.member_count, created=created,
            revision=revision))  # fmt: skip
    return 0


def run_restore(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    settings = resolved.values
    path = args.passphrase_file or settings.backup_passphrase_file
    if path is None:
        return _fail(t("backup.failed_passphrase_missing"))
    try:
        report = restore_archive(
            args.archive, args.destination, path, live_data_dir=settings.data_dir
        )
    except PassphraseError as error:
        return _fail(t("backup.passphrase_refused", detail=error))
    except BackupArchiveError as error:
        return _fail(t("backup.damaged", detail=error))
    except DestinationError as error:
        return _fail(DESTINATION_TEXT[error.reason](str(args.destination)))
    except OSError as error:
        return _fail(t("backup.restore_incomplete", path=args.destination, detail=error.strerror))
    print(t("backup.restored", path=report.destination, files=report.files))
    print(t("backup.next_steps", path=report.destination))
    return 0
