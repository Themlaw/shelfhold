"""``shelfhold trash empty --confirm empty-trash``: « Vider la corbeille » (EF-BIB-030,
EF-INT-014). A reserved action: the host CLI only (never an API key). Every bin of the library
roots goes at once, whatever its age, except what a hold keeps (an irreplaceable Unit, an open
intent, a retained backup, an adopted original with no verified copy), which is counted and said.
The word is typed again, and the server compares it: this command only carries it.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

REFUSED = 3


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("trash", help=t("trash.help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="trash_command", required=True)
    empty = actions.add_parser("empty", help=t("trash.empty_help"), allow_abbrev=False)
    empty.add_argument("--confirm", default=None, help=t("trash.confirm_help"))
    empty.set_defaults(handler=run_empty, configure_logging=False)


@ops_command
def _empty_trash(args: argparse.Namespace, channel: OpsChannel) -> int:
    report = channel.call("ops.trash_empty", typed=args.confirm)
    print(t("trash.done", removed=report["removed"], kept=report["kept"]))
    if report["held_irreplaceable"]:
        print(t("trash.held_irreplaceable", count=report["held_irreplaceable"]), file=sys.stderr)
    if report["held_other"]:
        print(t("trash.held_other", count=report["held_other"]), file=sys.stderr)
    return REFUSED if report["failed"] else 0


def run_empty(args: argparse.Namespace, resolved: object) -> int:
    if not args.confirm:
        print(t("trash.needs_confirmation"), file=sys.stderr)
        return 2
    return _empty_trash(args, resolved)  # type: ignore[arg-type]
