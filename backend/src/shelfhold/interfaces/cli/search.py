"""``shelfhold search rebuild``: the search index again, from the database alone (EF-BIB-010).

The index is a projection: nothing is kept in it that the catalog does not hold, so it is made
again whenever it is lost, stale or made by a SQLite that had no FTS5 yet. Reads and writes the
database directly, like the host operator who runs it.
"""

from __future__ import annotations

import argparse
import json
import sys

from shelfhold.catalog.public import ensure_search_tables, rebuild_search_index, search_capability
from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.migrate import current_revision, head_revision


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    search = commands.add_parser("search", help=t("cli.search_help"), allow_abbrev=False)
    actions = search.add_subparsers(dest="search_command", required=True)
    again = actions.add_parser("rebuild", help=t("cli.search_rebuild_help"), allow_abbrev=False)
    again.add_argument("--json", action="store_true", help=t("cli.json_help"))
    again.set_defaults(handler=run_rebuild, configure_logging=False)


def run_rebuild(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    database = resolved.values.database_path
    if not database.exists() or current_revision(database) != head_revision():
        print(t("status.database_not_migrated"), file=sys.stderr)
        return 1
    engine = create_db_engine(database)
    try:
        with write_transaction(engine) as connection:
            ensure_search_tables(connection)
            indexed = rebuild_search_index(connection)
            capability = search_capability(connection)
    finally:
        engine.dispose()
    if args.json:
        print(
            json.dumps(
                {"indexed": indexed, "mode": capability.mode, "trigram": capability.trigram},
                sort_keys=True,
            )
        )
        return 0
    print(t("search.rebuilt", count=indexed, mode=capability.mode))
    if capability.degraded:
        print(t("status.search_like_fallback"))
    return 0
