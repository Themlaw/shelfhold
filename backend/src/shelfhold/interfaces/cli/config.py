"""``shelfhold config show [--json]``."""

from __future__ import annotations

import argparse
import json

from pydantic import SecretStr

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t

MASK = "***"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    config = commands.add_parser("config", help=t("cli.config_help"))
    show = config.add_subparsers(dest="config_command", required=True).add_parser(
        "show", help=t("cli.config_show_help")
    )
    show.add_argument("--json", action="store_true", help=t("cli.json_help"))
    show.set_defaults(handler=run_show, configure_logging=False)


def _display(resolved: ResolvedSettings, name: str) -> object:
    value = getattr(resolved.values, name)
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return MASK
    return str(value) if not isinstance(value, str | int) else value


def run_show(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    rows = [
        {
            "name": name,
            "value": _display(resolved, name),
            "source": state.source,
            "readonly": state.readonly,
        }
        for name, state in resolved.fields.items()
    ]
    if args.json:
        payload = {"schema_version": resolved.schema_version, "fields": rows}
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    for row in rows:
        shown = {"name": row["name"], "value": row["value"], "source": row["source"]}
        print(t("config.row_readonly", **shown) if row["readonly"] else t("config.row", **shown))
    return 0
