"""``shelfhold detach --restore-upstream``: give Suwayomi back its settings (EF-EXP-056).

The files Shelfhold wrote and the files that were adopted stay where they are, and so does every
reading position: this deletes nothing. ``--full --target <id>`` is the full detachment of a Kavita
target (EF-EXP-031, a reserved action: it erases the hidden files, gives the library its settings
back and revokes the key; the exported files stay).

The command can be run again: a series that could not be restored keeps its follow and its
snapshot for the next run.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli import _adopt_ops as adopt_ops
from shelfhold.interfaces.cli._detach_ops import run_detach_full


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    detach = commands.add_parser("detach", help=t("detach.help"), allow_abbrev=False)
    detach.add_argument("--restore-upstream", action="store_true", help=t("detach.restore_help"))
    detach.add_argument("--full", action="store_true", help=t("detach.full_help"))
    detach.add_argument("--target", default=None, help=t("detach.target_help"))
    detach.add_argument("--confirm", default=None, help=t("detach.confirm_help"))
    detach.add_argument("--password-file", default=None, help=t("detach.password_file_help"))
    detach.add_argument("--discard-hidden-data", action="store_true", help=t("detach.discard_help"))
    detach.add_argument(
        "--discard-tombstone-journal", action="store_true", help=t("detach.discard_tombstone_help")
    )
    detach.add_argument("--give-up", action="store_true", help=t("detach.give_up_help"))
    detach.add_argument("--shelf", default=None, help=t("detach.shelf_help"))
    detach.add_argument("--series", default=None, help=t("detach.series_help"))
    detach.set_defaults(handler=run, configure_logging=False)


def run(args: argparse.Namespace, resolved: object) -> int:
    if args.full:
        if not args.target:
            import sys

            print(t("detach.needs_target"), file=sys.stderr)
            return 2
        return run_detach_full(args, resolved)  # type: ignore[arg-type]
    if not args.restore_upstream:
        import sys

        print(t("detach.needs_restore_upstream"), file=sys.stderr)
        return 2
    return adopt_ops.run_detach(args, resolved)  # type: ignore[arg-type]
