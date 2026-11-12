"""``shelfhold coverage resplit propose|apply``: a chapter the source cut again (EF-BIB-044, L26).

`propose` shows what would be covered and the code that confirms it; it writes nothing. `apply`
acts only with that code (`--confirm`), once, and removes nothing. A reserved action: the host
CLI or a fresh admin session, never a key.
"""

from __future__ import annotations

import argparse
import shlex

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

NEEDS_CONFIRMATION = 2


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("coverage", help=t("ops.help_coverage"), allow_abbrev=False)
    kinds = group.add_subparsers(dest="coverage_command", required=True)
    resplit = kinds.add_parser("resplit", help=t("ops.help_coverage_resplit"), allow_abbrev=False)
    actions = resplit.add_subparsers(dest="resplit_command", required=True)
    for name, help_text, handler in (
        ("propose", t("ops.help_coverage_resplit_propose"), run_propose),
        ("apply", t("ops.help_coverage_resplit_apply"), run_apply),
    ):
        leaf = actions.add_parser(name, help=help_text, allow_abbrev=False)
        leaf.add_argument("parent", help=t("ops.help_coverage_parent"))
        leaf.add_argument("children", nargs="*", help=t("ops.help_coverage_children"))
        if name == "apply":
            leaf.add_argument("--confirm", default=None, help=t("ops.help_coverage_confirm"))
        leaf.set_defaults(handler=handler, configure_logging=False)


@ops_command
def run_propose(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.coverage_resplit_propose", parent=args.parent, children=args.children)
    print(t("ops.resplit_proposed", parent=done["parent"], count=len(done["children"])))
    print(t("ops.resplit_confirm_with", parent=done["parent"],
            children=shlex.join(done["children"]), code=done["code"]))  # fmt: skip
    return 0


@ops_command
def run_apply(args: argparse.Namespace, channel: OpsChannel) -> int:
    if not args.confirm:
        print(t("ops.resplit_needs_confirm"))
        return NEEDS_CONFIRMATION
    done = channel.call("ops.coverage_resplit_apply", parent=args.parent,
                        children=args.children, confirm=args.confirm)  # fmt: skip
    if done["added"]:
        print(t("ops.resplit_applied", parent=done["parent"], added=done["added"]))
    else:
        print(t("ops.resplit_nothing", parent=done["parent"], added=done["added"]))
    return 0
