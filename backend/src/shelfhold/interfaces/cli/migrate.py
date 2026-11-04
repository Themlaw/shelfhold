"""``shelfhold migrate``."""

from __future__ import annotations

import argparse
import sys

import structlog

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.infrastructure.migrate import (
    MigrationError,
    MigrationInProgressError,
    SchemaTooNewError,
    migrate,
)


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    commands.add_parser("migrate", help=t("cli.migrate_help")).set_defaults(handler=run)


def run(_args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    log = structlog.get_logger()
    try:
        result = migrate(resolved.values)
    except SchemaTooNewError as exc:
        print(t("migrate.schema_too_new", revision=exc.revision), file=sys.stderr)
        return 1
    except MigrationInProgressError:
        print(t("migrate.in_progress"), file=sys.stderr)
        return 1
    except MigrationError as exc:
        print(t("migrate.failed", revision=exc.revision, detail=exc.detail), file=sys.stderr)
        return 1
    if not result.applied:
        print(t("migrate.up_to_date"))
        return 0
    if result.backup:
        print(t("migrate.backup", path=result.backup))
    print(t("migrate.applied", count=len(result.applied), revision=result.to_revision))
    log.info("migrate_done", revision=result.to_revision)
    return 0
