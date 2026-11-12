"""``shelfhold import scan [--dry-run] [--purchase]``: take in the files of the import folder
(EF-SRC-012).

A dry run shows what the folder holds and changes nothing. A scan stores what is ready and removes
a file from the folder only after its copy was read back with the same hash; a refused file stays
where it is, with its message. Nothing here tries to lift a protection.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.cli.import_tree import add_tree
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("import", help=t("ops.help_import"), allow_abbrev=False)
    actions = group.add_subparsers(dest="import_command", required=True)
    scan = actions.add_parser("scan", help=t("ops.help_import_scan"), allow_abbrev=False)
    scan.add_argument("--dry-run", action="store_true", help=t("ops.help_import_dry_run"))
    scan.add_argument("--purchase", action="store_true", help=t("ops.help_import_purchase"))
    scan.set_defaults(handler=run_scan, configure_logging=False)
    add_tree(actions)


def _message(code: str | None) -> str:
    return t(f"errors.{code}.title") if code else ""


@ops_command
def run_scan(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.import_scan", dry_run=args.dry_run, purchase=args.purchase)
    for path in answer["ready"]:
        print(t("ops.import_ready", path=path))
    for entry in answer["waiting"]:
        print(t("ops.import_waiting", path=entry["path"], message=_message(entry["code"])))
    for entry in answer["refused"]:
        print(t("ops.import_refused", path=entry["path"], message=_message(entry["code"])))
    stored = 0
    waiting = len(answer["waiting"])
    refused = len(answer["refused"])
    for done in answer["outcomes"]:
        if done["state"] == "stored":
            stored += 1
            line = t("ops.import_stored", path=done["path"], kind=done["kind"],
                     removed=t("ops.import_removed") if done["removed"] else "")  # fmt: skip
            if not done["removed"] and done["code"]:
                line = line + t("ops.import_kept", message=_message(done["code"]))
            if done["watermark"]:
                line += t("ops.import_watermark", origin=done["watermark"])
            print(line)
        elif done["state"] == "refused":
            refused += 1
            print(t("ops.import_refused", path=done["path"], message=_message(done["code"])))
        else:
            waiting += 1
            print(t("ops.import_waiting", path=done["path"], message=_message(done["code"])))
    if not args.dry_run:
        print(t("ops.import_summary", stored=stored, refused=refused, waiting=waiting))
    return 0
