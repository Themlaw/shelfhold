"""``shelfhold export-all FOLDER`` and ``shelfhold import-all FOLDER`` (EF-INT-014, EF-INT-016).

`export-all` writes the whole library in the open format `shelfhold-export/1` (versioned JSON and
the file tree) into a new or empty folder, without a tombstone, a secret or anything a tombstone
covers. `import-all` brings such a folder into a fresh instance, and refuses one that holds
anything. Both are reserved actions (the host CLI or a fresh admin session, never a key) and are
audited. The folder is named by absolute path: the server that may answer has another working
directory.
"""

from __future__ import annotations

import argparse
import os

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    out = commands.add_parser("export-all", help=t("exportall.export_help"), allow_abbrev=False)
    out.add_argument("folder", help=t("exportall.folder_help"))
    out.set_defaults(handler=run_export, configure_logging=False)
    back = commands.add_parser("import-all", help=t("exportall.import_help"), allow_abbrev=False)
    back.add_argument("folder", help=t("exportall.import_folder_help"))
    back.set_defaults(handler=run_import, configure_logging=False)


@ops_command
def run_export(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.export_all", folder=os.path.abspath(args.folder))
    print(
        t(
            "exportall.exported",
            folder=done["folder"],
            rows=done["rows"],
            tables=done["tables"],
            files=done["files"],
        )
    )
    if done["withheld_units"] or done["withheld_works"]:
        print(
            t(
                "exportall.withheld",
                units=done["withheld_units"],
                works=done["withheld_works"],
            )
        )
    if done["files_missing"] or done["files_off_registry"]:
        print(
            t(
                "exportall.files_warning",
                missing=done["files_missing"],
                altered=done["files_off_registry"],
            )
        )
    return 0


@ops_command
def run_import(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.import_all", folder=os.path.abspath(args.folder))
    print(t("exportall.imported", rows=done["rows"], tables=done["tables"], files=done["files"]))
    print(t("exportall.next_steps"))
    return 0
