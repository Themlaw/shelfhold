"""``shelfhold tombstone accept-lost-keyring|set-aside-journal|rekey|flush`` (R62, EF-BIB-053).

`accept-lost-keyring` (the keyring is gone for good), `set-aside-journal` (a journal that cannot
be read, or that was cut short, is put aside, never deleted, and written again from the table)
and `rekey` (after a key leak, EF-BIB-054 (10)) are reserved actions: the host CLI only, audited.
`flush` writes the lines a root owed while it was away.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("tombstone", help=t("ops.help_tombstone"), allow_abbrev=False)
    actions = group.add_subparsers(dest="tombstone_command", required=True)
    waive = actions.add_parser(
        "accept-lost-keyring", help=t("ops.help_accept_lost_keyring"), allow_abbrev=False
    )
    waive.add_argument("--confirm", required=True, help=t("ops.help_confirm_phrase"))
    waive.set_defaults(handler=run_accept, configure_logging=False)
    aside = actions.add_parser(
        "set-aside-journal", help=t("ops.help_set_aside_journal"), allow_abbrev=False
    )
    aside.add_argument("--root", dest="root_id", default="", help=t("ops.help_aside_root"))
    aside.set_defaults(handler=run_set_aside, configure_logging=False)
    rekey = actions.add_parser("rekey", help=t("ops.help_tombstone_rekey"), allow_abbrev=False)
    rekey.add_argument(
        "--kid", dest="kids", action="append", required=True, help=t("ops.help_rekey_kid")
    )
    rekey.set_defaults(handler=run_rekey, configure_logging=False)
    lift = actions.add_parser("lift", help=t("ops.help_tombstone_lift"), allow_abbrev=False)
    lift.add_argument("--seq", type=int, required=True, help=t("ops.help_lift_seq"))
    lift.add_argument("--confirm", default=None, help=t("ops.help_lift_confirm"))
    lift.set_defaults(handler=run_lift, configure_logging=False)
    listing = actions.add_parser("list", help=t("ops.help_tombstone_list"), allow_abbrev=False)
    listing.set_defaults(handler=run_list, configure_logging=False)
    flush = actions.add_parser("flush", help=t("ops.help_tombstone_flush"), allow_abbrev=False)
    flush.set_defaults(handler=run_flush, configure_logging=False)


@ops_command
def run_accept(args: argparse.Namespace, channel: OpsChannel) -> int:
    waived = channel.call("ops.tombstone_accept_lost_keyring", confirmation=args.confirm)["waived"]
    print(t("ops.keyring_waived", count=len(waived)))
    return 0


@ops_command
def run_set_aside(args: argparse.Namespace, channel: OpsChannel) -> int:
    wanted = {"root_id": args.root_id} if args.root_id else {}  # an empty id is no id
    aside = channel.call("ops.tombstone_set_aside_journal", **wanted)["aside"]
    for root_id, name in sorted(aside.items()):
        print(t("ops.journal_set_aside", root=root_id, name=name))
    if not aside:
        print(t("ops.journal_nothing_to_set_aside"))
    return 0


@ops_command
def run_rekey(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.tombstone_rekey", kids=",".join(args.kids))
    print(t("ops.tombstone_rekeyed", rows=done["table_rows"], roots=len(done["rewritten"])))
    for root_id in done["owed"]:
        print(t("ops.tombstone_rekey_owed", root=root_id))
    return 0


def run_lift(args: argparse.Namespace, resolved: object) -> int:
    if args.seq < 1:
        print(t("ops.lift_seq_invalid"), file=sys.stderr)
        return 2
    if not args.confirm:
        print(t("ops.lift_needs_confirmation", seq=args.seq), file=sys.stderr)
        return 2
    return _lift(args, resolved)  # type: ignore[arg-type]


@ops_command
def _lift(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.tombstone_lift", seq=args.seq, typed=args.confirm)
    print(t("ops.tombstone_lifted", seq=done["seq"]))
    if done.get("linked"):
        print(t("ops.tombstone_lifted_linked", seqs=", ".join(str(n) for n in done["linked"])))
    if done["pending"]:
        print(t("ops.tombstone_lift_owed", roots=done["pending"]), file=sys.stderr)
    return 0


@ops_command
def run_list(_args: argparse.Namespace, channel: OpsChannel) -> int:
    rows = channel.call("ops.tombstone_list")["tombstones"]
    for row in rows:
        print(
            t(
                "ops.tombstone_row",
                seq=row["seq"],
                day=str(row["created_at"])[:10],
                scope=row["scope"],
                reason=row["reason"] or "-",
            )
            + (
                t("ops.tombstone_row_from", seqs=", ".join(str(n) for n in row["linked_to"]))
                if row.get("linked_to")
                else ""
            )
        )
    if not rows:
        print(t("ops.tombstone_none"))
    return 0


@ops_command
def run_flush(_args: argparse.Namespace, channel: OpsChannel) -> int:
    still = channel.call("ops.tombstone_flush")["still_owed"]
    print(t("ops.lines_still_owed", count=len(still)) if still else t("ops.lines_flushed"))
    return 0
