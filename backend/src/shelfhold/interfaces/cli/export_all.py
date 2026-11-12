"""``shelfhold export-all``: full open export in ``shelfhold-export/1`` (EF-INT-016)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.platform.export_all import export_all


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cmd = commands.add_parser(
        "export-all",
        help=t("cli.export_all_help", default="Export the full library in the open format"),
        allow_abbrev=False,
    )
    cmd.add_argument("output", nargs="?", type=Path, default=None, help="Output directory")
    cmd.add_argument(
        "--out", "-o", dest="output_opt", type=Path, default=None, help="Output directory"
    )
    cmd.set_defaults(handler=run)


def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    output = args.output_opt or args.output
    if output is None:
        print("export-all: an output directory is required", file=sys.stderr)
        return 2
    settings = resolved.values
    engine = create_db_engine(settings.database_path)
    try:
        export_all(engine, settings.data_dir, settings.library_dir, settings.store_dir, output)
    finally:
        engine.dispose()
    print(str(output))
    return 0
