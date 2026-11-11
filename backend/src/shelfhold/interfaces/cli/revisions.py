"""``shelfhold revisions list|apply``: the revisions recorded without replacing a file (EF-BIB-043).

`list` shows the queue: what would be rewritten, how many files already read would start again in
KOReader, and the token of the batch. `apply` pushes the metadata-only revisions through the
Kavita API (no file touched) and, with `--confirm <token>`, asks for the rewrites; Units already
read are skipped unless `--include-read`. A revision the suspect-revision guard quarantined is
applied only with `--force-suspect <id>` (the id `list` shows, one per revision), a reserved
action of its own (EF-BIB-024). `apply` is a
reserved action: the host CLI or a fresh session, never a key.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel, OpsRefusal

NEEDS_CONFIRMATION = 3


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("revisions", help=t("ops.help_revisions"), allow_abbrev=False)
    actions = group.add_subparsers(dest="revisions_command", required=True)
    listing = actions.add_parser("list", help=t("ops.help_revisions_list"), allow_abbrev=False)
    listing.set_defaults(handler=run_list, configure_logging=False)
    apply = actions.add_parser("apply", help=t("ops.help_revisions_apply"), allow_abbrev=False)
    apply.add_argument("--confirm", default=None, help=t("ops.help_revisions_confirm"))
    apply.add_argument(
        "--include-read", action="store_true", help=t("ops.help_revisions_include_read")
    )
    apply.add_argument(
        "--force-suspect",
        action="append",
        default=[],
        metavar="ID",
        help=t("ops.help_revisions_force"),
    )
    apply.set_defaults(handler=run_apply, configure_logging=False)


@ops_command
def run_list(_args: argparse.Namespace, channel: OpsChannel) -> int:
    listed = channel.call("ops.revisions_list")
    for item in listed["items"]:
        print(
            t("ops.revision_line", id=item["id"], unit=item["unit_id"], revision=item["revision"],
              nature=item["nature"],
              flags=t("ops.revision_flag_sep").join(
                  mark
                  for mark, on in (
                      (t("ops.revision_suspect_mark", reason=item["suspect"]), item["suspect"]),
                      (t("ops.revision_read_mark"), item["koreader_reset"]),
                  )
                  if on
              ))
        )  # fmt: skip
    if not listed["items"]:
        print(t("ops.revisions_none"))
        return 0
    print(t("ops.revisions_impact", count=listed["koreader_positions"]))
    print(t("ops.revisions_token", token=listed["token"]))
    return 0


@ops_command
def run_apply(args: argparse.Namespace, channel: OpsChannel) -> int:
    method = "ops.revisions_force" if args.force_suspect else "ops.revisions_apply"
    try:
        done = channel.call(
            method,
            confirm=args.confirm,
            include_read=args.include_read,
            force_ids=list(args.force_suspect),
        )
    except OpsRefusal as refusal:
        if refusal.code != "revisions.confirmation_required":
            raise
        print(t("ops.revisions_needs_confirm", token=str(refusal)), file=sys.stderr)
        return NEEDS_CONFIRMATION
    print(
        t("ops.revisions_applied", api=done["metadata_applied"], requested=done["requested"],
          read=done["skipped_read"], suspect=done["skipped_suspect"])
    )  # fmt: skip
    return 0
