"""``shelfhold shelf create --max-age 13+|18+|X --target --subpath`` (EF-INT-024, EF-EXP-002) and
``shelfhold shelf withdraw [--shelf ID] [--apply --confirm VERSION]`` (EF-BIB-037).

A shelf below 18+ is a family shelf. `withdraw` prints the plan of what the admission refuses there
and changes nothing; `--apply --confirm` with the version of that plan takes the files out of the
library of the children (a reserved action: the host's console, or a fresh admin session).
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._headless import headless_command
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import HeadlessCommands, OpsChannel


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
    withdraw = actions.add_parser("withdraw", help=t("cli.shelf_withdraw_help"), allow_abbrev=False)
    withdraw.add_argument("--shelf", default=None, help=t("cli.shelf_withdraw_shelf_help"))
    withdraw.add_argument("--adopted", default=None, metavar="DESTINATION",
                          help=t("cli.shelf_withdraw_adopted_help"))  # fmt: skip
    withdraw.add_argument("--apply", action="store_true", help=t("cli.shelf_withdraw_apply_help"))
    withdraw.add_argument("--confirm", default=None, help=t("cli.shelf_withdraw_confirm_help"))
    withdraw.set_defaults(handler=run_withdraw, configure_logging=False)
    purge = actions.add_parser(
        "purge-upstream", help=t("cli.shelf_purge_upstream_help"), allow_abbrev=False
    )
    purge.add_argument("shelf", help=t("cli.shelf_purge_upstream_shelf_help"))
    switch = purge.add_mutually_exclusive_group(required=True)
    switch.add_argument("--on", dest="enabled", action="store_true", default=None,
                        help=t("cli.shelf_purge_upstream_on_help"))  # fmt: skip
    switch.add_argument("--off", dest="enabled", action="store_false",
                        help=t("cli.shelf_purge_upstream_off_help"))  # fmt: skip
    purge.set_defaults(handler=run_purge_upstream, configure_logging=False)


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


@ops_command
def run_purge_upstream(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.shelf_purge_upstream", shelf=args.shelf, enabled=args.enabled)
    if done["enabled"]:
        print(t("cli.shelf_purge_upstream_on", shelf=args.shelf))
    else:
        print(t("cli.shelf_purge_upstream_off", shelf=args.shelf))
    return 0


def _scope(args: argparse.Namespace) -> str:
    return f" --adopted {args.adopted}" if args.adopted else ""


@ops_command
def run_withdraw(args: argparse.Namespace, channel: OpsChannel) -> int:
    plan = channel.call("ops.withdraw_plan", shelf_id=args.shelf, destination_id=args.adopted)
    if not args.apply:
        for item in plan["items"]:
            print(
                t("cli.shelf_withdraw_item", edition=item["edition_id"], shelf=item["shelf_id"],
                  reason=item["reason"], files=len(item["files"]))
            )  # fmt: skip
        if not plan["items"]:
            print(t("cli.shelf_withdraw_nothing"))
            return 0
        print(t("cli.shelf_withdraw_next", version=plan["version"], scope=_scope(args)))
        return 0
    if args.confirm != plan["version"]:  # typed from the plan that was read, never assumed
        print(t("cli.shelf_withdraw_needs_confirm", version=plan["version"]), file=sys.stderr)
        return 2
    done = channel.call(
        "ops.withdraw_apply",
        if_match=args.confirm,
        shelf_id=args.shelf,
        destination_id=args.adopted,
    )
    print(t("cli.shelf_withdraw_done", editions=len(done["withdrawn"]), files=done["files"]))
    if done["pending"]:
        print(t("cli.shelf_withdraw_pending", count=len(done["pending"])), file=sys.stderr)
    return 0
