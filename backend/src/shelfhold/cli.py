"""Command line entry point: `shelfhold migrate | serve | config show`."""

import argparse
import json
import os
import sys
from collections.abc import Sequence

import structlog
import uvicorn
from pydantic import SecretStr

from shelfhold.config import ConfigError, ResolvedSettings, load_settings
from shelfhold.i18n import t
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.migrate import (
    MigrationError,
    MigrationInProgressError,
    SchemaTooNewError,
    check_schema_not_newer,
    migrate,
)
from shelfhold.infrastructure.settings_store import load_db_settings

MASK = "***"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shelfhold", description=t("cli.description"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help=t("cli.migrate_help"))
    commands.add_parser("serve", help=t("cli.serve_help"))
    config = commands.add_parser("config", help=t("cli.config_help"))
    show = config.add_subparsers(dest="config_command", required=True).add_parser(
        "show", help=t("cli.config_show_help")
    )
    show.add_argument("--json", action="store_true", help=t("cli.json_help"))
    return parser


def _resolve() -> ResolvedSettings:
    return load_settings(os.environ, db_loader=load_db_settings)


def _display(resolved: ResolvedSettings, name: str) -> object:
    value = getattr(resolved.values, name)
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return MASK
    return str(value) if not isinstance(value, str | int) else value


def _show_config(resolved: ResolvedSettings, as_json: bool) -> None:
    rows = [
        {
            "name": name,
            "value": _display(resolved, name),
            "source": state.source,
            "readonly": state.readonly,
        }
        for name, state in resolved.fields.items()
    ]
    if as_json:
        payload = {"schema_version": resolved.schema_version, "fields": rows}
        print(json.dumps(payload, ensure_ascii=False))
        return
    for row in rows:
        shown = {"name": row["name"], "value": row["value"], "source": row["source"]}
        print(t("config.row_readonly", **shown) if row["readonly"] else t("config.row", **shown))


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        resolved = _resolve()
    except ConfigError as exc:
        print(t("config.invalid", detail=exc), file=sys.stderr)
        return 2
    settings = resolved.values
    for path in resolved.world_readable_secret_files:
        print(t("config.secret_file_world_readable", path=path), file=sys.stderr)

    if args.command == "config":
        _show_config(resolved, args.json)
        return 0

    configure_logging(level=settings.log_level, log_format=settings.log_format)
    if args.command == "serve":
        try:
            check_schema_not_newer(settings)  # refuse early, with a clear message
        except SchemaTooNewError as exc:
            print(t("migrate.schema_too_new", revision=exc.revision), file=sys.stderr)
            return 1
        uvicorn.run(
            "shelfhold.main:app_from_env",
            factory=True,
            host=settings.host,
            port=settings.port,
            log_config=None,
            access_log=False,
            server_header=False,
        )
        return 0

    log = structlog.get_logger()
    try:
        result = migrate(settings)
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
