"""``shelfhold detach --restore-upstream``: give Suwayomi back its settings (EF-EXP-056).

The files Shelfhold wrote and the files that were adopted stay where they are, and so does every
reading position: this deletes nothing (the full detachment that removes files is another,
reserved action). The command can be run again: a series that could not be restored keeps its
follow and its snapshot for the next run.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli import _adopt_ops as adopt_ops


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    detach = commands.add_parser("detach", help=t("detach.help"), allow_abbrev=False)
    detach.add_argument("--restore-upstream", action="store_true", help=t("detach.restore_help"))
    detach.add_argument("--shelf", default=None, help=t("detach.shelf_help"))
    detach.add_argument("--series", default=None, help=t("detach.series_help"))
    detach.set_defaults(handler=run, configure_logging=False)


def run(args: argparse.Namespace, resolved: object) -> int:
    if not args.restore_upstream:
        import sys

        print(t("detach.needs_restore_upstream"), file=sys.stderr)
        return 2
    return adopt_ops.run_detach(args, resolved)  # type: ignore[arg-type]
