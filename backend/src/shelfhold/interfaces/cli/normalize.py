"""``shelfhold normalize migrate <normalizer>``: the explicit migration of a normalizer's rows to
its current version (ENF-OPS-021), with the tombstone check of EF-BIB-054 (4).

Without ``--confirm`` it only previews: the rows that would move, the merges to decide, the rows a
tombstone blocks, and the fingerprint of that plan. With ``--confirm <fingerprint>`` it applies
exactly that plan (a stale fingerprint changes nothing, exit code 4). A reserved action: the host
CLI or a fresh session, never a key.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel, OpsRefusal

STALE_PREVIEW = 4


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("normalize", help=t("ops.help_normalize"), allow_abbrev=False)
    actions = group.add_subparsers(dest="normalize_command", required=True)
    migrate = actions.add_parser(
        "migrate", help=t("ops.help_normalize_migrate"), allow_abbrev=False
    )
    migrate.add_argument("name", help=t("ops.help_normalizer"))
    migrate.add_argument(
        "--confirm", metavar="FINGERPRINT", default=None, help=t("ops.help_normalize_confirm")
    )
    migrate.set_defaults(handler=run_migrate, configure_logging=False)


@ops_command
def run_migrate(args: argparse.Namespace, channel: OpsChannel) -> int:
    try:
        done = channel.call("ops.normalize_migrate", name=args.name, confirm=args.confirm)
    except OpsRefusal as refusal:
        if refusal.code != "ops.preview_stale":
            raise
        print(t("ops.normalize_preview_stale"), file=sys.stderr)
        return STALE_PREVIEW
    target = done["target"]
    held = done["migrate"] + done["merge"] + done["blocked"] + done["unresolved"]
    if not held:
        print(t("ops.normalize_up_to_date", target=target))
        return 0
    print(
        t("ops.normalize_header", target=target, migrate=len(done["migrate"]),
          merge=len(done["merge"]), blocked=len(done["blocked"]),
          unresolved=len(done["unresolved"]))
    )  # fmt: skip
    for outcome in ("migrate", "merge", "blocked", "unresolved"):
        for row in done[outcome]:
            print(t("ops.normalize_row", outcome=outcome, id=row["id"], old=row["old"] or "-",
                    new=row["new"] or "-", reason=row["reason"]))  # fmt: skip
    for group in done["collisions"]:
        print(t("ops.normalize_collision", ids=str(tuple(group))))
    if done["blocked"]:
        print(t("ops.normalize_blocked_hint"))
    if done["applied"] is None:
        print(t("ops.normalize_fingerprint", fingerprint=done["fingerprint"]))
    else:
        print(t("ops.normalize_applied", applied=done["applied"], target=target))
    return 0
