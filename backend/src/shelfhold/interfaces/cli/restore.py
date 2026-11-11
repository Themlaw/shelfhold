"""``shelfhold restore acknowledge``: the exit of ``restored_credentials``.

A restore revokes every session and API key (a credential revoked after the backup would be live
again otherwise). The passkeys and the passwords changed since the backup are back as they were:
look at them (the passkeys page, ``admin reset-password``), then say so here. A reserved action;
until it is run, ``status`` lists the wait, whatever restarts happen.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("restore", help=t("ops.help_restore"), allow_abbrev=False)
    actions = group.add_subparsers(dest="restore_command", required=True)
    acknowledge = actions.add_parser(
        "acknowledge", help=t("ops.help_restore_acknowledge"), allow_abbrev=False
    )
    acknowledge.set_defaults(handler=run_acknowledge, configure_logging=False)


@ops_command
def run_acknowledge(_args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call("ops.restore_acknowledge")
    print(t("ops.restore_acknowledged"))
    return 0
