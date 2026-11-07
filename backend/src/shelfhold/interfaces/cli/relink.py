"""``shelfhold relink <work>``: attach the new binding of a moved series to a Work that exists
(EF-BIB-029, EF-INT-014).

After a source migration in Suwayomi the series is followed under another source. The command
binds that source to the Work already in the catalog, keeps its Team and the aliases of the old
binding, and lists the numbers the Edition lacks. Nothing is fetched here: the next follow cycle
asks for those and for nothing else. An Edition that is new for a rated Work is not filed on a
shelf below 18+; it waits in "À classer" (EF-BIB-055).
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    relink = commands.add_parser("relink", help=t("ops.help_relink"), allow_abbrev=False)
    relink.add_argument("work_id", help=t("ops.help_relink_work"))
    relink.add_argument("--source-key", required=True, help=t("ops.help_relink_source_key"))
    relink.add_argument("--remote-id", required=True, help=t("ops.help_relink_remote_id"))
    relink.add_argument("--replaces", default=None, help=t("ops.help_relink_replaces"))
    relink.add_argument("--lang", default=None, help=t("ops.help_relink_lang"))
    relink.set_defaults(handler=run, configure_logging=False)


@ops_command
def run(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call(
        "ops.relink", work_id=args.work_id, source_key=args.source_key,
        remote_id=args.remote_id, replaces=args.replaces, lang=args.lang,
    )  # fmt: skip
    print(t("ops.relinked", edition=answer["edition_id"], binding=answer["binding_id"]))
    missing = answer["missing"]
    if missing:
        print(t("ops.relink_missing", count=len(missing), numbers=",".join(missing)))
    else:
        print(t("ops.relink_nothing_missing"))
    if answer["to_rate"]:
        print(t("ops.relink_to_rate", edition=answer["edition_id"]))
    return 0
