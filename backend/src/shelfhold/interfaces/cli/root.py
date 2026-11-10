"""``shelfhold root takeover|reinit|restored <root>``: the exits of a root that is not right
(EF-EXP-064, EF-INT-023).

``takeover`` and ``reinit`` are reserved (the host CLI or a fresh session, never a key);
``restored``
is not. None of them deletes a file: ``takeover`` stamps the root as ours and keeps everything,
``reinit`` also says that the disk lost what the database remembers (the records go to ``missing``),
``restored`` plans for the next export what the old copy lacks. ``<root>`` is the ``root_id`` that
``shelfhold status`` prints, or the absolute path of a folder.
"""

from __future__ import annotations

import argparse
from typing import Any

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

__all__ = ["print_report", "register"]


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("root", help=t("ops.help_root"), allow_abbrev=False)
    actions = group.add_subparsers(dest="root_command", required=True)
    for name, handler, help_text in (
        ("takeover", run_takeover, t("ops.help_root_takeover")),
        ("reinit", run_reinit, t("ops.help_root_reinit")),
        ("restored", run_restored, t("ops.help_root_restored")),
    ):
        leaf = actions.add_parser(name, help=help_text, allow_abbrev=False)
        leaf.add_argument("root", help=t("ops.help_root_arg"))
        leaf.set_defaults(handler=handler, configure_logging=False)


def print_report(done: dict[str, Any]) -> None:
    print(t("ops.root_done", command=done["command"], root=done["root_id"], path=done["path"],
            previous=done["previous"]))  # fmt: skip
    if done["predecessors"]:
        print(t("ops.root_predecessors", count=len(done["predecessors"])))
    if done["missing"]:
        print(t("ops.root_missing", count=done["missing"]))
    if done["planned"]:
        print(t("ops.root_planned", count=done["planned"]))
    if done["units_store_missing"]:
        print(t("ops.store_units_missing", count=done["units_store_missing"]))
    for claim in done["ignored_claims"]:
        print(t("ops.root_ignored_claim", claim=claim))


@ops_command
def run_takeover(args: argparse.Namespace, channel: OpsChannel) -> int:
    print_report(channel.call("ops.root_takeover", root=args.root))
    return 0


@ops_command
def run_reinit(args: argparse.Namespace, channel: OpsChannel) -> int:
    print_report(channel.call("ops.root_reinit", root=args.root))
    return 0


@ops_command
def run_restored(args: argparse.Namespace, channel: OpsChannel) -> int:
    print_report(channel.call("ops.root_restored", root=args.root))
    return 0
