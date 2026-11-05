"""``shelfhold series resume``: after the switch-over, follow and export the series again."""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("series", help=t("ops.help_series"), allow_abbrev=False)
    actions = group.add_subparsers(dest="series_command", required=True)
    resume = actions.add_parser("resume", help=t("ops.help_series_resume"), allow_abbrev=False)
    resume.add_argument("export_series_id")
    resume.set_defaults(handler=run_resume, configure_logging=False)


@ops_command
def run_resume(args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call("ops.series_resume", export_series_id=args.export_series_id)
    print(t("ops.done"))
    return 0
