"""``shelfhold status``: warnings an operator should see (review PR #41; more in later lots)."""

from __future__ import annotations

import argparse
import json

from shelfhold.config import ResolvedSettings
from shelfhold.headless.applied import applied_public_url
from shelfhold.i18n import t
from shelfhold.identity.public import IDENTITY_HEADER_AUDIT, local_auth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.infrastructure.volumes import data_dir_writable_by_others


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    status = commands.add_parser("status", help=t("cli.status_help"), allow_abbrev=False)
    status.add_argument("--json", action="store_true", help=t("cli.json_help"))
    status.set_defaults(handler=run, configure_logging=False)


def _environment_warnings(resolved: ResolvedSettings) -> list[dict[str, str]]:
    settings = resolved.values
    found: list[dict[str, str]] = []
    if data_dir_writable_by_others(settings.data_dir):
        found.append(
            {
                "code": "data_dir_writable_by_others",
                "message": t("status.data_dir_writable_by_others", path=settings.data_dir),
            }
        )
    if settings.cli_allow_host:  # EF-USR-024: the host's Shelfhold uid is an admin
        found.append({"code": "cli_allow_host", "message": t("status.cli_allow_host")})
    return found


def warnings(resolved: ResolvedSettings) -> list[dict[str, str]]:
    settings = resolved.values
    database = settings.database_path
    if not database.exists() or current_revision(database) != head_revision():
        return [
            *_environment_warnings(resolved),
            {"code": "database_not_migrated", "message": t("status.database_not_migrated")},
        ]
    engine = create_db_engine(database)
    try:
        headers = local_auth(engine, settings).recent_audit_targets(IDENTITY_HEADER_AUDIT)
        applied = applied_public_url(engine)
    finally:
        engine.dispose()
    pending: list[dict[str, str]] = []
    if applied is not None and applied != (settings.public_url or ""):
        pending.append({"code": "public_url_restart", "message": t("status.public_url_restart")})
    return (
        _environment_warnings(resolved)
        + pending
        + [
            {
                "code": "identity_header_stripped",
                "message": t("status.identity_header_stripped", header=header),
            }
            for header in headers
        ]
    )


def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    found = warnings(resolved)
    if args.json:
        print(json.dumps({"warnings": found}, ensure_ascii=False, indent=2))
        return 0
    if not found:
        print(t("status.no_warning"))
    for warning in found:
        print(t("status.warning_line", message=warning["message"]))
    return 0
