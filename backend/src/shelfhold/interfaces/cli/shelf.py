"""``shelfhold shelf create --max-age 18+|X --target --subpath`` (EF-INT-024, EF-EXP-002).

Only an adult shelf in M1a: a lower ceiling is refused with "available with M1b".
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._headless import headless_command
from shelfhold.interfaces.local_cli import HeadlessCommands


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("shelf", help=t("cli.shelf_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="shelf_command", required=True)
    create = actions.add_parser("create", help=t("cli.shelf_create_help"), allow_abbrev=False)
    create.add_argument("name", help=t("cli.shelf_name_help"))
    create.add_argument("--max-age", required=True, help=t("cli.shelf_max_age_help"))
    create.add_argument("--target", required=True, help=t("cli.shelf_target_help"))
    create.add_argument("--subpath", default="", help=t("cli.shelf_subpath_help"))
    create.add_argument("--phrase", help=t("cli.shelf_phrase_help"))
    create.set_defaults(handler=run_create, configure_logging=False)


@headless_command
def run_create(args: argparse.Namespace, commands: HeadlessCommands) -> int:
    created = commands.shelf_create(
        name=args.name, max_age=args.max_age, target=args.target, subpath=args.subpath,
        phrase=args.phrase,
    )  # fmt: skip
    if created.created:
        print(t("headless.shelf_created", name=args.name))
    else:
        print(t("headless.shelf_exists", name=args.name))
    return 0
