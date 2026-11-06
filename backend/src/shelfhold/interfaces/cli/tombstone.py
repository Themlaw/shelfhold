"""``shelfhold tombstone accept-lost-keyring|set-aside-journal|flush`` (R62, EF-BIB-053).

`accept-lost-keyring` (the keyring is gone for good) and `set-aside-journal` (a journal that
cannot be read is put aside, never deleted) are reserved actions: the host CLI only, audited.
`flush` writes the lines a root owed while it was away.
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
    aside = actions.add_parser(
        "set-aside-journal", help=t("ops.help_set_aside_journal"), allow_abbrev=False
    )
    aside.add_argument("--root", dest="root_id", default="", help=t("ops.help_aside_root"))
    aside.set_defaults(handler=run_set_aside, configure_logging=False)
    flush = actions.add_parser("flush", help=t("ops.help_tombstone_flush"), allow_abbrev=False)
    flush.set_defaults(handler=run_flush, configure_logging=False)


@ops_command
def run_accept(args: argparse.Namespace, channel: OpsChannel) -> int:
    waived = channel.call("ops.tombstone_accept_lost_keyring", confirmation=args.confirm)["waived"]
    print(t("ops.keyring_waived", count=len(waived)))
    return 0


@ops_command
def run_set_aside(args: argparse.Namespace, channel: OpsChannel) -> int:
    aside = channel.call("ops.tombstone_set_aside_journal", root_id=args.root_id)["aside"]
    for root_id, name in sorted(aside.items()):
        print(t("ops.journal_set_aside", root=root_id, name=name))
    if not aside:
        print(t("ops.journal_nothing_to_set_aside"))
    return 0


@ops_command
def run_flush(_args: argparse.Namespace, channel: OpsChannel) -> int:
    still = channel.call("ops.tombstone_flush")["still_owed"]
    print(t("ops.lines_still_owed", count=len(still)) if still else t("ops.lines_flushed"))
    return 0
