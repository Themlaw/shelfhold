"""``shelfhold store reinit``: the exit of ``store_lost`` (EF-EXP-064).

``/store`` is not what the database remembers (a new disk, a copy of another instance's folder, a
restore without it). The command stamps it as ours, and the Units whose kept copy lived there go
to ``store_missing``. A reserved action; it deletes nothing and downloads nothing.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.cli.root import print_report
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("store", help=t("ops.help_store"), allow_abbrev=False)
    actions = group.add_subparsers(dest="store_command", required=True)
    reinit = actions.add_parser("reinit", help=t("ops.help_store_reinit"), allow_abbrev=False)
    reinit.set_defaults(handler=run_reinit, configure_logging=False)


@ops_command
def run_reinit(_args: argparse.Namespace, channel: OpsChannel) -> int:
    print_report(channel.call("ops.store_reinit"))
    return 0
