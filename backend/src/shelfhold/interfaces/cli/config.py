"""``shelfhold config show|get|set|list|apply`` (EF-INT-024).

``show`` prints the start-up settings (env, config.toml, database); ``get|set|list`` read and
write the keys of the database layer; ``apply -f shelfhold.toml`` imports a declarative file
(idempotent, ``--dry-run`` prints the differences). A secret key is never taken from argv.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import SecretStr

from shelfhold.config import ResolvedSettings
from shelfhold.headless import ConfigFileError, parse_desired
from shelfhold.headless.keys import SECRET_KEYS
from shelfhold.i18n import t
from shelfhold.interfaces.cli._headless import fail, headless_command, read_secret
from shelfhold.interfaces.local_cli import HeadlessCommands

MASK = "***"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    config = commands.add_parser("config", help=t("cli.config_help"), allow_abbrev=False)
    actions = config.add_subparsers(dest="config_command", required=True)
    show = actions.add_parser("show", help=t("cli.config_show_help"))
    show.add_argument("--json", action="store_true", help=t("cli.json_help"))
    show.set_defaults(handler=run_show, configure_logging=False)

    get = actions.add_parser("get", help=t("cli.config_get_help"), allow_abbrev=False)
    get.add_argument("key", help=t("cli.config_key_help"))
    get.set_defaults(handler=run_get, configure_logging=False)
    listing = actions.add_parser("list", help=t("cli.config_list_help"), allow_abbrev=False)
    listing.add_argument("--json", action="store_true", help=t("cli.json_help"))
    listing.set_defaults(handler=run_list, configure_logging=False)
    put = actions.add_parser("set", help=t("cli.config_set_help"), allow_abbrev=False)
    put.add_argument("key", help=t("cli.config_key_help"))
    put.add_argument("value", nargs="?", help=t("cli.config_value_help"))
    put.add_argument("--value-file", help=t("cli.config_value_file_help"))
    put.set_defaults(handler=run_set, configure_logging=False)
    apply = actions.add_parser("apply", help=t("cli.config_apply_help"), allow_abbrev=False)
    apply.add_argument("-f", "--file", required=True, help=t("cli.config_file_help"))
    apply.add_argument("--dry-run", action="store_true", help=t("cli.config_dry_run_help"))
    apply.set_defaults(handler=run_apply, configure_logging=False)


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


def _shown(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


@headless_command
def run_get(args: argparse.Namespace, commands: HeadlessCommands) -> int:
    row = commands.get(args.key)
    print("" if row.value is None else _shown(row.value))
    return 0


@headless_command
def run_list(args: argparse.Namespace, commands: HeadlessCommands) -> int:
    rows = commands.list()
    if args.json:
        payload = [
            {"key": r.key, "value": r.value, "source": r.source, "default": r.default} for r in rows
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print(t("headless.config_line", name=row.key, value=_shown(row.value), source=row.source))
    return 0


@headless_command
def run_set(args: argparse.Namespace, commands: HeadlessCommands) -> int:
    if args.key in SECRET_KEYS and args.value is not None:  # argv is visible in `ps`
        return fail(t("headless.secret_key_needs_file", name=args.key))
    if (args.value is None) == (args.value_file is None):
        return fail(t("headless.value_or_file"))
    try:
        value = args.value if args.value is not None else read_secret(args.value_file)
    except (OSError, UnicodeDecodeError) as error:
        return fail(t("headless.secret_unreadable", detail=error))
    row = commands.set(args.key, value)
    print(t("headless.config_set", name=row.key))
    if row.overridden_by is not None:
        print(t("headless.config_overridden", name=row.key, layer=row.overridden_by),
              file=sys.stderr)  # fmt: skip
    return 0


def _secret_files(text: str, base: Path) -> dict[str, str]:
    desired = parse_desired(text, base_dir=base)
    paths = [t.password_file for t in desired.targets] + [s.secret_file for s in desired.sources]
    return {str(p): read_secret(str(p)) for p in paths if p is not None}


@headless_command
def run_apply(args: argparse.Namespace, commands: HeadlessCommands) -> int:
    path = Path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
        secrets = _secret_files(text, path.resolve().parent)
    except (OSError, UnicodeDecodeError) as error:
        return fail(t("headless.file_unreadable", detail=error))
    except ConfigFileError as error:
        return fail(t("headless.refused", code=error.code, detail=error))
    plan = commands.apply_text(
        text, base_dir=path.resolve().parent, dry_run=args.dry_run, secrets=secrets
    )
    print(plan.render() or t("headless.nothing_to_apply"))
    print(t("headless.dry_run_done") if args.dry_run else t("headless.apply_done"), file=sys.stderr)
    return 0
