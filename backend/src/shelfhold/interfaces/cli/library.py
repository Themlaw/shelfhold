"""``shelfhold library normalize-pages [--dry-run]``: bring the pages of the archives already filed
to JPEG, PNG, GIF or WebP (EF-EXP-046, ADR-0054).

`--dry-run` lists the chapters concerned and changes nothing. Without it each chapter's original
goes to /store, then the library takes the normalized version; a chapter with a page that cannot be
read is left as it is and said so. The command can be run again after an interruption (a kill -9, a
full disk): it finds what is left and finishes it, and a library already normalized gives nothing
to do.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("library", help=t("ops.help_library"), allow_abbrev=False)
    actions = group.add_subparsers(dest="library_command", required=True)
    pages = actions.add_parser(
        "normalize-pages", help=t("ops.help_library_normalize_pages"), allow_abbrev=False
    )
    pages.add_argument("--dry-run", action="store_true", help=t("ops.help_pages_dry_run"))
    pages.set_defaults(handler=run_normalize_pages, configure_logging=False)


@ops_command
def run_normalize_pages(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.normalize_pages", dry_run=args.dry_run)
    if args.dry_run:
        for unit in answer["pending"]:
            print(t("ops.pages_pending", unit=unit))
        for item in answer["destination_files"]:
            print(t("ops.pages_destination_pending", item=item))
        if not answer["pending"] and not answer["destination_files"]:
            print(t("ops.pages_nothing"))
        return 0
    for done in answer["outcomes"]:
        if done["status"] == "normalized":
            print(t("ops.pages_normalized", unit=done["unit_id"]))
        elif done["status"] == "kept_as_received":
            print(t("ops.pages_kept", unit=done["unit_id"], codes=",".join(done["issues"])))
        elif done["status"] == "failed":
            print(t("ops.pages_failed", unit=done["unit_id"], code=done["code"]))
    for done in answer["destination_outcomes"]:
        if done["outcome"] == "upgraded":
            print(t("ops.pages_destination_rewritten", item=done["export_item_id"]))
        else:
            print(
                t(
                    "ops.pages_destination_left",
                    item=done["export_item_id"],
                    outcome=done["outcome"],
                )
            )
    if not answer["outcomes"] and not answer["destination_outcomes"]:
        print(t("ops.pages_nothing"))
    else:
        print(
            t("ops.pages_summary", normalized=answer["normalized"],
              kept=answer["kept_as_received"], failed=answer["failed"])
        )  # fmt: skip
    return 1 if answer["failed"] else 0
