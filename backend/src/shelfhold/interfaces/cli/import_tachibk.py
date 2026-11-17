"""``shelfhold import tachibk <file> [--profile <name>]`` and ``--apply <id>`` (EF-INT-008).

Takes in a Mihon backup: the series become pending source items (their source is resolved later,
nothing in the file is ever called), the chapters they list are known units, what the file proves
read is kept as read markers of the profile you name (yours by default), and the trackers as hints.

Two steps, as on the screen: the first command reads the file as a hostile one (bounded, in a
subprocess) and prints what it would write, writing nothing to the library; the second,
``--apply <id>``, writes it once. Both are the reserved action `import.tachibk`: the host's command
line is allowed it. No name of a source is ever printed or kept.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import fail, ops_command
from shelfhold.interfaces.local_cli import OpsChannel

REFUSED = 2


def add_tachibk(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    tachibk = actions.add_parser("tachibk", help=t("ops.help_import_tachibk"), allow_abbrev=False)
    tachibk.add_argument("path", nargs="?", help=t("ops.help_import_tachibk_path"))
    tachibk.add_argument("--profile", help=t("ops.help_import_tachibk_profile"))
    tachibk.add_argument(
        "--apply", dest="apply_id", metavar="ID", help=t("ops.help_import_tachibk_apply")
    )
    tachibk.set_defaults(handler=run_tachibk, configure_logging=False)


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Nothing to add at the top: `tachibk` joins the `import` group (one group, one name)."""


@ops_command
def _run(args: argparse.Namespace, channel: OpsChannel) -> int:
    if args.apply_id:
        done = channel.call("ops.import_tachibk_apply", id=args.apply_id)
        print(
            t(
                "ops.tachibk_applied",
                series=done["pending_items"],
                markers=done["markers"],
                hints=done["hints"],
            )
        )
        return 0
    view = channel.call("ops.import_tachibk_preview", path=args.path, profile=args.profile)
    print(
        t(
            "ops.tachibk_preview",
            series=view["series"],
            chapters=view["chapters"],
            read=view["read"],
            started=view["started"],
            trackers=view["trackers"],
        )
    )
    print(t("ops.tachibk_apply_with", id=view["id"]))
    return 0


def run_tachibk(args: argparse.Namespace, resolved: object) -> int:
    if bool(args.apply_id) == bool(args.path) or (args.apply_id and args.profile):
        return fail(t("ops.tachibk_usage"), REFUSED)
    return _run(args, resolved)  # type: ignore[arg-type]
