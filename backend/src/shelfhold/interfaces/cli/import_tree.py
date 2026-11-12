"""``shelfhold import tree <path> [--dry-run]``: take in an existing Suwayomi download tree
(EF-INT-009).

The Units are made from the files already on the disk, with nothing downloaded again, and attached
to the Suwayomi bindings that exist. The tree is only read: nothing in it is removed or changed.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def add_tree(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    tree = actions.add_parser("tree", help=t("ops.help_import_tree"), allow_abbrev=False)
    tree.add_argument("path", help=t("ops.help_import_tree_path"))
    tree.add_argument("--dry-run", action="store_true", help=t("ops.help_import_dry_run"))
    tree.set_defaults(handler=run_tree, configure_logging=False)


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Nothing to add at the top: `tree` joins the `import` group that `import_folder` makes
    (one group, one name), through `add_tree`."""


def _message(code: str | None) -> str:
    return t(f"errors.{code}.title") if code else ""


@ops_command
def run_tree(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.import_tree", path=args.path, dry_run=args.dry_run)
    for path in answer["ready"]:
        print(t("ops.import_ready", path=path))
    for entry in answer["waiting"]:
        print(t("ops.import_waiting", path=entry["path"], message=_message(entry["code"])))
    for entry in answer["refused"]:
        print(t("ops.import_refused", path=entry["path"], message=_message(entry["code"])))
    stored = skipped = 0
    refused, waiting = len(answer["refused"]), len(answer["waiting"])
    for done in answer["outcomes"]:
        if done["state"] == "stored":
            stored += 1
            print(t("ops.import_tree_stored", path=done["path"], kind=done["kind"]))
        elif done["state"] == "skipped":
            skipped += 1
            print(t("ops.import_tree_skipped", path=done["path"], message=_message(done["code"])))
        elif done["state"] == "refused":
            refused += 1
            print(t("ops.import_refused", path=done["path"], message=_message(done["code"])))
        else:
            waiting += 1
            print(t("ops.import_waiting", path=done["path"], message=_message(done["code"])))
    if not args.dry_run:
        counts = {"stored": stored, "skipped": skipped, "refused": refused, "waiting": waiting}
        print(t("ops.import_tree_summary", **counts))
    return 0
