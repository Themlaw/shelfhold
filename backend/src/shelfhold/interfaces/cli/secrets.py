"""``shelfhold secrets export|rotate|status`` (ENF-SEC-005).

The passphrase is read from a file (``--passphrase-file``); there is deliberately no option that
takes it on the command line, and abbreviations are off so ``--passphrase`` is not a shortcut.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shelfhold.config import ResolvedSettings, Settings
from shelfhold.i18n import t
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.infrastructure.secrets.atomic import write_private
from shelfhold.infrastructure.secrets.errors import (
    ExportError,
    KeyRetirementRefusedError,
    KeyringError,
    RotationInProgressError,
    SecretsError,
)
from shelfhold.infrastructure.secrets.export import encrypt_export, read_passphrase_file
from shelfhold.infrastructure.secrets.rotation import RotationIncompleteError, retire_key, rotate

STDOUT = "-"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("secrets", help=t("cli.secrets_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="secrets_command", required=True)

    export = actions.add_parser("export", help=t("cli.secrets_export_help"), allow_abbrev=False)
    export.add_argument(
        "--passphrase-file", required=True, type=Path, help=t("cli.secrets_passphrase_file_help")
    )
    export.add_argument("--out", required=True, help=t("cli.secrets_out_help"))
    export.set_defaults(handler=run_export, configure_logging=False)

    rotation = actions.add_parser("rotate", help=t("cli.secrets_rotate_help"), allow_abbrev=False)
    rotation.add_argument("--retire", metavar="KID", help=t("cli.secrets_retire_help"))
    rotation.set_defaults(handler=run_rotate)

    status = actions.add_parser("status", help=t("cli.secrets_status_help"), allow_abbrev=False)
    status.add_argument("--json", action="store_true", help=t("cli.json_help"))
    status.set_defaults(handler=run_status)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def run_export(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    settings = resolved.values
    try:
        passphrase = read_passphrase_file(args.passphrase_file)
        register_secret_value(passphrase)
        store = diagnostics.store_for(settings)
        existed = store.exists()
        keyring = diagnostics.boot_check(settings, announce=False)  # stdout may carry the export
        if not existed:
            print(t("secrets.keyring_generated", path=store.path), file=sys.stderr)
        armored = encrypt_export(keyring, passphrase)
        if args.out == STDOUT:
            sys.stdout.write(armored)
        else:
            write_private(Path(args.out), armored.encode("ascii"), overwrite=False)
    except ExportError as exc:
        return _fail(t("secrets.export_failed", detail=exc))
    except KeyringError as exc:
        return _fail(t("secrets.keyring_error", detail=exc))
    except FileExistsError:
        return _fail(t("secrets.out_exists", path=args.out))
    except OSError as exc:
        return _fail(t("secrets.write_failed", detail=exc.strerror or exc))
    try:  # remembered so `status` stops asking; a read-only keyring only costs the reminder
        with store.lock():
            store.write(store.load().exported())
    except (OSError, SecretsError):
        print(t("secrets.export_not_recorded"), file=sys.stderr)
    if args.out != STDOUT:
        print(t("secrets.exported", path=args.out))
    return 0


def _database_ready(settings: Settings) -> bool:
    return settings.database_path.exists()


def run_rotate(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    settings = resolved.values
    if not _database_ready(settings):
        return _fail(t("secrets.no_database"))
    store = diagnostics.store_for(settings)
    engine = create_db_engine(settings.database_path)
    try:
        diagnostics.boot_check(settings, create=False)
        if args.retire:
            retire_key(store, engine, args.retire)
            print(t("secrets.retired", kid=args.retire))
            return 0
        report = rotate(store, engine)
    except KeyRetirementRefusedError as exc:
        return _fail(_refusal_text(exc))
    except RotationInProgressError:
        return _fail(t("secrets.rotation_in_progress"))
    except RotationIncompleteError as exc:
        return _fail(t("secrets.rotation_incomplete", remaining=exc.remaining))
    except KeyringError as exc:
        return _fail(t("secrets.keyring_error", detail=exc))
    except SecretsError as exc:
        return _fail(t("secrets.rotation_failed", detail=exc))
    finally:
        engine.dispose()
    if report.resumed:
        print(t("secrets.rotation_resumed", kid=report.new_kid, count=report.reencrypted))
    else:
        print(t("secrets.rotated", kid=report.new_kid, count=report.reencrypted))
    print(t("secrets.export_again"))
    return 0


def _refusal_text(exc: KeyRetirementRefusedError) -> str:
    texts = {
        "unknown": t("secrets.retire_refused_unknown", kid=exc.kid),
        "active": t("secrets.retire_refused_active", kid=exc.kid),
        "in_use": t("secrets.retire_refused_in_use", kid=exc.kid),
        "cited_by_tombstone": t("secrets.retire_refused_cited_by_tombstone", kid=exc.kid),
        "root_unavailable": t("secrets.retire_refused_root_unavailable", kid=exc.kid),
    }
    return texts[exc.reason]


def warning_text(warning: diagnostics.KeyringWarning) -> str:
    detail = warning.detail
    texts = {
        "key_in_data": t("secrets.warning_key_in_data", detail=detail),
        "rows_on_retired_key": t("secrets.warning_rows_on_retired_key", detail=detail),
        "keyring_world_readable": t("secrets.warning_keyring_world_readable", detail=detail),
        "keyring_absent": t("secrets.warning_keyring_absent", detail=detail),
        "rotation_incomplete": t("secrets.warning_rotation_incomplete"),
        "never_exported": t("secrets.warning_never_exported"),
        "export_older_than_rotation": t("secrets.warning_export_older_than_rotation"),
        "tombstones_not_exported": t("secrets.warning_tombstones_not_exported"),
        "tombstone_kid_missing": t("secrets.warning_tombstone_kid_missing", detail=detail),
    }
    return texts[warning.code]


def run_status(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    try:
        warnings = diagnostics.collect_warnings(resolved.values)
    except KeyringError as exc:
        return _fail(t("secrets.keyring_error", detail=exc))
    if args.json:
        rows = [{"code": w.code, "detail": w.detail} for w in warnings]
        print(json.dumps({"warnings": rows}, ensure_ascii=False))
        return 0
    if not warnings:
        print(t("secrets.status_ok"))
    for warning in warnings:
        print(warning_text(warning))
    return 0
