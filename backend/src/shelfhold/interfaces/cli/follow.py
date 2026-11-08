"""``shelfhold follow`` and ``shelfhold unfollow`` (EF-SRC-029, EF-INT-014).

`follow` makes one entry in the connected service's library, in the `Shelfhold` category, without
automatic download; asking twice changes nothing. `unfollow` keeps the series in the service; with
`--remove-upstream` it leaves the library too, and that needs `--confirm` (exit code 3 otherwise,
with the exact command). The route and this command go through the same service.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

NEEDS_CONFIRMATION = 3


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    follow = commands.add_parser("follow", help=t("ops.help_follow_series"), allow_abbrev=False)
    _series(follow)
    follow.set_defaults(handler=run_follow, configure_logging=False)
    unfollow = commands.add_parser(
        "unfollow", help=t("ops.help_unfollow_series"), allow_abbrev=False
    )
    _series(unfollow)
    unfollow.add_argument(
        "--remove-upstream", action="store_true", help=t("ops.help_unfollow_remove_upstream")
    )
    unfollow.add_argument("--confirm", action="store_true", help=t("ops.help_unfollow_confirm"))
    unfollow.set_defaults(handler=run_unfollow, configure_logging=False)


def _series(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source_key", help=t("ops.help_follow_source_key"))
    parser.add_argument("remote_id", help=t("ops.help_follow_remote_id"))


@ops_command
def run_follow(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.follow", source_key=args.source_key, remote_id=args.remote_id)
    if answer["state"] == "in_progress":
        print(t("ops.follow_in_progress"))
    else:
        print(t("ops.follow_done") if answer["sent"] else t("ops.follow_already"))
    return 0


@ops_command
def _unfollow(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call(
        "ops.unfollow", source_key=args.source_key, remote_id=args.remote_id,
        remove_upstream=args.remove_upstream, confirm=args.confirm,
    )  # fmt: skip
    print(t("ops.unfollow_removed") if answer["removed"] else t("ops.unfollow_done"))
    return 0


def run_unfollow(args: argparse.Namespace, resolved: object) -> int:
    if args.remove_upstream and not args.confirm:
        print(
            t("ops.unfollow_needs_confirmation", source_key=args.source_key,
              remote_id=args.remote_id),
            file=sys.stderr,
        )  # fmt: skip
        return NEEDS_CONFIRMATION
    return _unfollow(args, resolved)  # type: ignore[arg-type]
