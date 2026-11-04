"""``shelfhold serve``."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.infrastructure.migrate import SchemaTooNewError, check_schema_not_newer


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    commands.add_parser("serve", help=t("cli.serve_help")).set_defaults(handler=run)


def run(_args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    settings = resolved.values
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
