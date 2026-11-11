"""``shelfhold diagnostic bundle --out <file> [--log-file <file>]...``: the minimal diagnostic
bundle (M2 L30, ENF-LEG-009, EF-BIB-041). Local CLI only (D8): it reads the database and the files
it is given, opens no connection and writes the one file it is asked for. The interface comes in M3.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.interfaces.cli import status
from shelfhold.ops.diagnostic import (
    DiagnosticInputRefusedError,
    DiagnosticLeakError,
    DiagnosticOutputExistsError,
    DiagnosticOutputRefusedError,
    build_bundle,
)


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("diagnostic", help=t("cli.diagnostic_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="diagnostic_action", required=True)
    bundle = actions.add_parser("bundle", help=t("cli.diagnostic_bundle_help"), allow_abbrev=False)
    bundle.add_argument("--out", required=True, type=Path, help=t("cli.diagnostic_out_help"))
    bundle.add_argument(
        "--log-file", action="append", default=[], type=Path, dest="log_files",
        help=t("cli.diagnostic_log_file_help"),
    )  # fmt: skip
    bundle.set_defaults(handler=run_bundle, configure_logging=False)


def run_bundle(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    out: Path = args.out
    if not out.parent.is_dir():
        print(t("diagnostic.out_dir_missing"), file=sys.stderr)
        return 2
    try:
        built = build_bundle(
            resolved,
            out,
            status=lambda: status.report(resolved),
            log_files=args.log_files,
            now=datetime.now(UTC),
        )
    except DiagnosticOutputExistsError:
        print(t("diagnostic.out_exists"), file=sys.stderr)
        return 2
    except DiagnosticOutputRefusedError:
        print(t("diagnostic.out_in_content_area"), file=sys.stderr)
        return 2
    except DiagnosticInputRefusedError as refused:
        print(
            t("diagnostic.input_refused", index=refused.index, reason=refused.reason),
            file=sys.stderr,
        )
        return 2
    except DiagnosticLeakError as leak:  # a bug of the positive list: never the value
        print(
            t("diagnostic.leak_refused", category=leak.category, member=leak.member),
            file=sys.stderr,
        )
        return 1
    print(
        t(
            "diagnostic.written", members=len(built.members), kept=built.log_lines_kept,
            dropped=built.log_lines_dropped,
        )
    )  # fmt: skip
    return 0
