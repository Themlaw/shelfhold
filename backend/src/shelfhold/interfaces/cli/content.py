"""``shelfhold content reneutralize``: regenerate the neutralized derivatives of `untrusted` EPUB
after a security update of the neutralizer (EF-BIB-051), in the manner of ENF-OPS-021.

Without ``--confirm`` it only previews: the derivatives made by another release, where each
regeneration restarts from (the original in /store, else the stored derivative), those that cannot
be regenerated, and the fingerprint of that plan. With ``--confirm <fingerprint>`` it applies
exactly that plan (a stale fingerprint changes nothing, exit code 4). Nothing is deleted: each file
is replaced atomically after the new one is verified, and the old generation goes to
`derived_chain`.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel, OpsRefusal

STALE_PREVIEW = 4


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("content", help=t("cli.content_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="content_command", required=True)
    rene = actions.add_parser(
        "reneutralize", help=t("cli.content_reneutralize_help"), allow_abbrev=False
    )
    rene.add_argument(
        "--confirm", metavar="FINGERPRINT", default=None, help=t("cli.content_confirm_help")
    )
    rene.set_defaults(handler=run_reneutralize, configure_logging=False)


@ops_command
def run_reneutralize(args: argparse.Namespace, channel: OpsChannel) -> int:
    try:
        done = channel.call("ops.content_reneutralize", confirm=args.confirm)
    except OpsRefusal as refusal:
        if refusal.code != "ops.preview_stale":
            raise
        print(t("content.preview_stale"), file=sys.stderr)
        return STALE_PREVIEW
    if not done["items"]:
        print(t("content.up_to_date", target=done["target"]))
        return 0
    print(t("content.header", target=done["target"], count=len(done["items"])))
    for item in done["items"]:
        print(t("content.row", unit=item["unit"], old=item["from"], source=item["source"],
                reason=item["reason"] or "-"))  # fmt: skip
    if done["applied"] is None:
        print(t("content.fingerprint", fingerprint=done["fingerprint"]))
        return 0
    print(t("content.applied", **done["applied"]))
    return 1 if done["applied"]["failed"] else 0
