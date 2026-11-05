"""``shelfhold probe <folder>``: can this container write, rename and make a folder there?

The check of EF-UX-002 (and the one `target recheck` makes in each series folder): when it fails,
the cause and the exact compose block that fixes it. The probe leaves nothing behind.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    probe = commands.add_parser("probe", help=t("probe.help"), allow_abbrev=False)
    probe.add_argument("folder")
    probe.set_defaults(handler=run, configure_logging=False)


@ops_command
def run(args: argparse.Namespace, channel: OpsChannel) -> int:
    report = channel.call("ops.probe", folder=args.folder)
    if report["ok"]:
        print(t("probe.ok", folder=report["folder"]))
        return 0
    print(t("probe.failed", step=report["failed_step"], cause=report["cause"]), file=sys.stderr)
    if report["yaml_hint"]:
        print(report["yaml_hint"], file=sys.stderr)
    return 1
