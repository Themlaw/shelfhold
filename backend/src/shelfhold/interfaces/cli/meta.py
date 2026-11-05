"""``shelfhold meta rebuild|check``: `unit.json` is a projection of the database (ENF-OPS-020).

The database is the authority; these files in `/data/meta` are regenerated from it, and a file of
a newer format than this build is reported and never rewritten (ENF-OPS-013).
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.library.public import MetaProjection


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("meta", help=t("cli.meta_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="meta_command", required=True)
    rebuild = actions.add_parser("rebuild", help=t("cli.meta_rebuild_help"), allow_abbrev=False)
    rebuild.add_argument("--unit", help=t("cli.meta_unit_help"))
    rebuild.set_defaults(handler=run_rebuild)
    check = actions.add_parser("check", help=t("cli.meta_check_help"), allow_abbrev=False)
    check.set_defaults(handler=run_check)


def _projection(resolved: ResolvedSettings) -> MetaProjection | None:
    settings = resolved.values
    if (
        not settings.database_path.exists()
        or current_revision(settings.database_path) != head_revision()
    ):
        print(t("status.database_not_migrated"), file=sys.stderr)
        return None
    return MetaProjection(create_db_engine(settings.database_path), settings.data_dir)


def run_rebuild(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    projection = _projection(resolved)
    if projection is None:
        return 1
    try:
        if args.unit is not None:
            if not is_ulid(args.unit) or projection.project(args.unit) == "missing_unit":
                print(t("meta.unknown_unit", unit=args.unit), file=sys.stderr)
                return 1
            return 0
        report = projection.repair()
    finally:
        projection.dispose()
    print(t("meta.rebuilt", written=len(report.written)))
    for unit_id in report.read_only:
        print(t("meta.newer_format", unit=unit_id))
    return 0


def run_check(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    projection = _projection(resolved)
    if projection is None:
        return 1
    try:
        report = projection.repair(dry_run=True)
    finally:
        projection.dispose()
    for unit_id in report.written:
        print(t("meta.drift", unit=unit_id))
    for unit_id in report.read_only:
        print(t("meta.newer_format", unit=unit_id))
    return 1 if report.written else 0
