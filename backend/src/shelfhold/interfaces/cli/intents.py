"""``shelfhold intents list|resolve``: the file operations an interruption left open (EF-INT-014).

`resolve` is a reserved action (the host CLI or a fresh session, never a key) and is audited. By
default it applies the rule of the boot to the one intent: the operation is completed or replayed
only when the disk matches exactly one known step, otherwise the intent stays open and nothing is
touched (exit code 3). `--abandon --confirm <id>` gives the intent up without touching the disk.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel, OpsRefusal

STILL_OPEN = 3


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("intents", help=t("ops.help_intents"), allow_abbrev=False)
    actions = group.add_subparsers(dest="intents_command", required=True)
    listing = actions.add_parser("list", help=t("ops.help_intents_list"), allow_abbrev=False)
    listing.set_defaults(handler=run_list, configure_logging=False)
    resolve = actions.add_parser("resolve", help=t("ops.help_intents_resolve"), allow_abbrev=False)
    resolve.add_argument("intent_id", help=t("ops.help_intent_id"))
    resolve.add_argument("--abandon", action="store_true", help=t("ops.help_intents_abandon"))
    resolve.add_argument("--confirm", default=None, help=t("ops.help_intents_confirm"))
    resolve.set_defaults(handler=run_resolve, configure_logging=False)


@ops_command
def run_list(_args: argparse.Namespace, channel: OpsChannel) -> int:
    items = channel.call("ops.intents_list")["items"]
    for item in items:
        print(
            t("ops.intent_line", id=item["id"], op=item["op"], state=item["state"],
              dst=item["dst"] or item["src"] or "-", created=item["created_at"] or "-",
              stale=t("ops.intent_stale_mark") if item["stale_from_backup"] else "")
        )  # fmt: skip
    if not items:
        print(t("ops.intents_none"))
    return 0


@ops_command
def _resolve(args: argparse.Namespace, channel: OpsChannel) -> int:
    try:
        done = channel.call(
            "ops.intents_resolve", intent_id=args.intent_id,
            action="abandon" if args.abandon else "replay", confirm=args.confirm,
        )  # fmt: skip
    except OpsRefusal as refusal:
        if refusal.code != "ops.still_waiting":
            raise
        print(
            t("ops.intent_still_open", id=args.intent_id, code=refusal.code, detail=refusal),
            file=sys.stderr,
        )
        return STILL_OPEN
    print(t("ops.intent_resolved", id=done["intent_id"], outcome=done["outcome"]))
    return 0


def run_resolve(args: argparse.Namespace, resolved: object) -> int:
    if args.abandon and not args.confirm:
        print(t("ops.intent_needs_confirm"), file=sys.stderr)
        return 2
    return _resolve(args, resolved)  # type: ignore[arg-type]
