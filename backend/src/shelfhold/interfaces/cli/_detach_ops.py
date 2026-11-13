"""``target remove`` and ``detach --full`` (M3 L30, EF-EXP-031), over the operating channel.

Both are reserved actions and both go in two steps: the command first prints what would be lost
and the phrase to type, then runs again with ``--confirm "<phrase>"`` to do it. Nothing is
changed by the first call. The password of the dedicated Kavita account (to revoke its key) is
read from a file or from stdin, never from an argument.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._headless import fail, read_secret
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

__all__ = ["run_detach_full", "run_remove"]

NEEDS_CONFIRMATION = 2
UNFINISHED = 3


def _print_warnings(prefix: str, warnings: list[dict[str, object]]) -> None:
    for warning in warnings:
        print(t(f"{prefix}.warning_{warning['code']}", count=warning["count"]))


@ops_command
def run_remove(args: argparse.Namespace, channel: OpsChannel) -> int:
    mode = "keep_files" if args.keep_files else "delete_files"
    if args.give_up:
        report = channel.call(
            "ops.target_remove", destination_id=args.destination_id, mode=mode, give_up=True
        )
        print(t("removal.given_up", count=report["cancelled"]))
        return 0
    if not args.confirm:  # a first call changes nothing: it shows and asks
        preview = channel.call(
            "ops.target_remove_preview", destination_id=args.destination_id, mode=mode
        )
        _print_warnings("removal", preview["warnings"])
        print(t("removal.type_phrase", phrase=preview["phrase"]), file=sys.stderr)
        return NEEDS_CONFIRMATION
    report = channel.call(
        "ops.target_remove", destination_id=args.destination_id, mode=mode,
        typed=args.confirm, give_up=False,
    )  # fmt: skip
    print(t("removal.done", deleted=report["deleted"], forgotten=report["forgotten"]))
    for line in report["left"]:
        print(t("removal.left", code=line["code"], path=line["path"]), file=sys.stderr)
    return 0 if report["complete"] else UNFINISHED


@ops_command
def run_detach_full(args: argparse.Namespace, channel: OpsChannel) -> int:
    if args.give_up:
        report = channel.call(
            "ops.detach_full", target_id=args.target, give_up=True, discard_hidden_data=False
        )
        print(t("detach.given_up", count=report["cancelled"]))
        return 0
    if not args.confirm:
        preview = channel.call("ops.detach_full_preview", target_id=args.target)
        _print_warnings("detach", preview["warnings"])
        print(t("detach.type_phrase", phrase=preview["phrase"]), file=sys.stderr)
        return NEEDS_CONFIRMATION
    credential = None
    if args.password_file:
        try:
            credential = read_secret(args.password_file)
        except (OSError, UnicodeDecodeError) as error:
            return fail(t("headless.secret_unreadable", detail=error))
    report = channel.call(
        "ops.detach_full", target_id=args.target, typed=args.confirm, credential=credential,
        discard_hidden_data=args.discard_hidden_data, give_up=False,
    )  # fmt: skip
    if report["hidden_removed"]:
        print(t("detach.hidden_removed", count=report["hidden_removed"]))
    if any(s["exclude_patterns_restored"] for s in report["settings"]):
        print(t("detach.settings_restored"))
    print(t("detach.key_revoked") if report["key_revoked"] else t("detach.key_kept"))
    for line in report["leftovers"]:
        stream = (
            sys.stderr
            if line["code"].endswith(("_not_erased", "_not_restored", "_not_revoked"))
            else sys.stdout
        )
        print(t(f"detach.leftover_{line['code']}", count=line["count"]), file=stream)
    print(t("detach.done_complete") if report["complete"] else t("detach.done_incomplete"))
    return 0 if report["complete"] else UNFINISHED
