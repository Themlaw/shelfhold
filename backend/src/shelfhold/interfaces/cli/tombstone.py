"""``shelfhold tombstone accept-lost-keyring``: the keyring is gone for good (R62, EF-BIB-053).

A reserved action: the host CLI only (never an API key), a typed confirmation, audited.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("tombstone", help=t("ops.help_tombstone"), allow_abbrev=False)
    actions = group.add_subparsers(dest="tombstone_command", required=True)
    waive = actions.add_parser(
        "accept-lost-keyring", help=t("ops.help_accept_lost_keyring"), allow_abbrev=False
    )
    waive.add_argument("--confirm", required=True, help=t("ops.help_confirm_phrase"))
    waive.set_defaults(handler=run_accept, configure_logging=False)


@ops_command
def run_accept(args: argparse.Namespace, channel: OpsChannel) -> int:
    waived = channel.call("ops.tombstone_accept_lost_keyring", confirmation=args.confirm)["waived"]
    print(t("ops.keyring_waived", count=len(waived)))
    return 0
