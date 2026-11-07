"""``shelfhold access-table``: who may do each reserved action, generated from the registry.

The truth table of ENF-SEC-010 (action x role x credential) with its `fresh_auth_s` column
(EF-USR-022), under the instance's own settings (`fresh_auth_s`, hardened profile). Read-only: it
opens no database and reaches no network. `--restricted-profile` plays the case where a
restricted profile exists (parental control then asks for a fresh session).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import UTC, datetime

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.identity.public import truth_table


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    table = commands.add_parser("access-table", help=t("cli.access_table_help"), allow_abbrev=False)
    table.add_argument(
        "--restricted-profile", action="store_true", help=t("cli.access_restricted_help")
    )
    table.add_argument("--json", action="store_true", help=t("cli.json_help"))
    table.set_defaults(handler=run, configure_logging=False)


def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    settings = resolved.values
    rows = truth_table(
        now=datetime.now(UTC),
        fresh_auth_s=settings.fresh_auth_s,
        restricted_profile_exists=args.restricted_profile,
        hardened=settings.auth_hardened,
    )
    if args.json:
        print(json.dumps([vars(row) for row in rows], sort_keys=True))
        return 0
    writer = csv.writer(sys.stdout, dialect="excel-tab", lineterminator=os.linesep)
    for row in rows:
        fresh = "-" if row.fresh_auth_s is None else str(row.fresh_auth_s)
        writer.writerow([row.action, row.role, row.credential, row.outcome, fresh])
    return 0
