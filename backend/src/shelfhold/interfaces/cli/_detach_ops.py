"""``target remove`` and ``detach --full`` (M3 L30, EF-EXP-031), over the operating channel.

Both are reserved actions and both go in two steps: the command first prints what would be lost
and the phrase to type, then runs again with ``--confirm "<phrase>"`` to do it. Nothing is
changed by the first call. The password of the dedicated Kavita account (to revoke its key) is
read from a file or from stdin, never from an argument.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from shelfhold.i18n import t
from shelfhold.interfaces.cli._headless import fail, read_secret
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel, OpsRefusal

__all__ = ["run_detach_full", "run_remove"]

NEEDS_CONFIRMATION = 2
UNFINISHED = 3


_WARNINGS: dict[str, Callable[[object], str]] = {
    "removal:files_kept": lambda n: t("removal.warning_files_kept", count=n),
    "removal:manifest_forgotten": lambda n: t("removal.warning_manifest_forgotten", count=n),
    "removal:files_deleted": lambda n: t("removal.warning_files_deleted", count=n),
    "removal:past_the_bin": lambda n: t("removal.warning_past_the_bin", count=n),
    "removal:adopted_kept": lambda n: t("removal.warning_adopted_kept", count=n),
    "detach:files_kept": lambda n: t("detach.warning_files_kept", count=n),
    "detach:hidden_erased": lambda n: t("detach.warning_hidden_erased", count=n),
    "detach:key_revoked": lambda n: t("detach.warning_key_revoked", count=n),
    "detach:manifest_forgotten": lambda n: t("detach.warning_manifest_forgotten", count=n),
    "detach:hidden_data_present": lambda n: t("detach.warning_hidden_data_present", count=n),
    "detach:root_shared": lambda n: t("detach.warning_root_shared", count=n),
    "detach:follows_active": lambda n: t("detach.warning_follows_active", count=n),
    "detach:tombstone_journal_erased": lambda n: t(
        "detach.warning_tombstone_journal_erased", count=n
    ),
}
_LEFTOVERS: dict[str, Callable[[object], str]] = {
    "hidden_pattern_kept": lambda n: t("detach.leftover_hidden_pattern_kept", count=n),
    "key_id_reused": lambda n: t("detach.leftover_key_id_reused", count=n),
    "erasing_name_taken": lambda n: t("detach.leftover_erasing_name_taken", count=n),
    "instance_changed": lambda n: t("detach.leftover_instance_changed", count=n),
    "key_gone": lambda n: t("detach.leftover_key_gone", count=n),
    "library_gone": lambda n: t("detach.leftover_library_gone", count=n),
    "tombstone_journal_erased": lambda n: t("detach.leftover_tombstone_journal_erased", count=n),
    "suwayomi_downloads": lambda n: t("detach.leftover_suwayomi_downloads", count=n),
    "kavita_accounts": lambda n: t("detach.leftover_kavita_accounts", count=n),
    "store": lambda n: t("detach.leftover_store", count=n),
    "root_shared": lambda n: t("detach.leftover_root_shared", count=n),
    "hidden_not_erased": lambda n: t("detach.leftover_hidden_not_erased", count=n),
    "settings_not_restored": lambda n: t("detach.leftover_settings_not_restored", count=n),
    "key_not_revoked": lambda n: t("detach.leftover_key_not_revoked", count=n),
    "library_type_changed": lambda n: t("detach.leftover_library_type_changed", count=n),
}


def _print_warnings(prefix: str, warnings: list[dict[str, object]]) -> None:
    for warning in warnings:
        print(_WARNINGS[f"{prefix}:{warning['code']}"](warning["count"]))


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
        try:
            report = channel.call(
                "ops.detach_full",
                target_id=args.target,
                give_up=True,
                discard_hidden_data=False,
                discard_tombstone_journal=False,
                key_gone=False,
            )
        except OpsRefusal as refusal:
            if str(refusal) != "past_no_return":
                raise
            print(t("detach.give_up_too_late"), file=sys.stderr)  # said, never "0 abandoned"
            return UNFINISHED
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
        discard_hidden_data=args.discard_hidden_data,
        discard_tombstone_journal=args.discard_tombstone_journal, key_gone=args.key_gone,
        give_up=False,
    )  # fmt: skip
    if report["hidden_removed"]:
        print(t("detach.hidden_removed", count=report["hidden_removed"]))
    if report["settings_restored"]:
        print(t("detach.settings_restored"))
    if report["key_gone"]:
        print(t("detach.key_gone"))
    else:
        print(t("detach.key_revoked") if report["key_revoked"] else t("detach.key_kept"))
    for line in report["leftovers"]:
        stream = (
            sys.stderr
            if line["code"].endswith(
                ("_not_erased", "_not_restored", "_not_revoked", "_name_taken", "_changed")
            )
            else sys.stdout
        )
        print(_LEFTOVERS[line["code"]](line["count"]), file=stream)
    print(t("detach.done_complete") if report["complete"] else t("detach.done_incomplete"))
    return 0 if report["complete"] else UNFINISHED
