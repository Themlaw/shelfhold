"""``shelfhold missing list|resolve``: what to do with a file that vanished (EF-INT-023)."""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import fail, ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("missing", help=t("ops.help_missing"), allow_abbrev=False)
    actions = group.add_subparsers(dest="missing_command", required=True)
    listing = actions.add_parser("list", help=t("ops.help_missing_list"), allow_abbrev=False)
    listing.set_defaults(handler=run_list, configure_logging=False)
    resolve = actions.add_parser("resolve", help=t("ops.help_missing_resolve"), allow_abbrev=False)
    resolve.add_argument("destination_id")
    resolve.add_argument("export_item_id")
    resolve.add_argument("--reexport", action="store_true", help=t("ops.help_reexport"))
    resolve.add_argument("--unfollow", action="store_true", help=t("ops.help_unfollow"))
    resolve.set_defaults(handler=run_resolve, configure_logging=False)


@ops_command
def run_list(_args: argparse.Namespace, channel: OpsChannel) -> int:
    items = channel.call("ops.missing_list")["items"]
    if not items:
        print(t("ops.nothing_missing"))
    for item in items:
        print(t("ops.missing_line", item=item["subject"], reason=item["reason"]))
    return 0


def run_resolve(args: argparse.Namespace, resolved: object) -> int:
    if args.reexport == args.unfollow:  # neither or both: nothing is decided
        return fail(t("ops.choose_one"), code=2)
    return _resolve(args, resolved)  # type: ignore[arg-type]


@ops_command
def _resolve(args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call(
        "ops.missing_resolve", destination_id=args.destination_id,
        export_item_id=args.export_item_id, action="reexport" if args.reexport else "unfollow",
    )  # fmt: skip
    print(t("ops.done"))
    return 0
