"""``shelfhold stats --since 30d``: value indicators, computed on the instance (EF-INT-025).

Reads the database directly, like the host operator who runs it; opens no network connection
and writes nothing (ENF-PRIV-001).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.infrastructure.stats import (
    DEFAULT_SINCE,
    InvalidSinceError,
    compute_stats,
    parse_since,
)


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    stats = commands.add_parser("stats", help=t("cli.stats_help"), allow_abbrev=False)
    stats.add_argument("--since", default=DEFAULT_SINCE, help=t("cli.stats_since_help"))
    stats.add_argument("--json", action="store_true", help=t("cli.json_help"))
    stats.set_defaults(handler=run, configure_logging=False)


def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    try:
        days = parse_since(args.since)
    except InvalidSinceError:
        print(t("stats.invalid_since", value=args.since), file=sys.stderr)
        return 2
    database = resolved.values.database_path
    if not database.exists() or current_revision(database) != head_revision():
        print(t("status.database_not_migrated"), file=sys.stderr)
        return 1
    engine = create_db_engine(database)
    try:
        result = compute_stats(engine, now=datetime.now(UTC), since_days=days)
    finally:
        engine.dispose()
    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0
    delay = result.median_detection_to_visible_s
    print(t("stats.header", days=days))
    print(t("stats.waiting_states", count=result.human_interventions["waiting_states"]))
    print(t("stats.replays", count=result.human_interventions["needs_review_replays"]))
    print(t("stats.units_exported", count=result.units_exported))
    print(t("stats.median_delay", value="-" if delay is None else delay))
    print(
        t(
            "stats.guards",
            sentinel=result.guards_triggered["sentinel"],
            writer=result.guards_triggered["foreign_writer"],
        )
    )
    return 0
