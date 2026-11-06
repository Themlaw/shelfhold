"""``shelfhold adopt``: adopt an existing adult Kavita library (EF-INT-014, EF-EXP-028).

`--dry-run` shows what adoption would do and writes nothing (not in the database, not on disk, not
in Kavita). Nothing is entered without `--confirm`. The existing files are referenced where they
are: never rewritten, moved, copied or deleted; the renames shown are proposals only.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.config import ResolvedSettings
from shelfhold.export.targets.kavita.adopt import AdoptionReport, AdoptionSession, PreviewStale
from shelfhold.i18n import t
from shelfhold.interfaces.cli import _adopt_ops as adopt_ops

__all__ = ["AdoptionSession", "register", "run"]

REFUSED = 3
STALE = 4


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    adopt = commands.add_parser("adopt", help=t("adopt.help"), allow_abbrev=False)
    adopt.add_argument("--dry-run", action="store_true", help=t("adopt.dry_run_help"))
    adopt.add_argument(
        "--confirm", metavar="FINGERPRINT", default=None, help=t("adopt.confirm_help")
    )
    adopt.add_argument("--destination", default=None, help=t("adopt.destination_help"))
    adopt.add_argument("--library", default=None, help=t("adopt.library_help"))
    adopt.add_argument("--repatriate", action="store_true", help=t("adopt.repatriate_help"))
    adopt.add_argument("--source", default=None, help=t("adopt.source_help"))
    adopt.add_argument("--pair-auto", action="store_true", help=t("adopt.pair_auto_help"))
    adopt.add_argument("--min-score", type=float, default=0.9, help=t("adopt.min_score_help"))
    adopt.add_argument(
        "--confirm-migration", action="store_true", help=t("adopt.confirm_migration_help")
    )
    adopt.set_defaults(handler=run, configure_logging=False)
    steps = adopt.add_subparsers(dest="adopt_command", required=False)
    pair = steps.add_parser("pair", help=t("adopt.pair_help"), allow_abbrev=False)
    pair.add_argument("--destination", dest="sub_destination", default=None)
    pair.add_argument("--series", default=None, help=t("adopt.pair_series_help"))
    pair.add_argument("--source-key", default=None, help=t("adopt.pair_source_key_help"))
    pair.add_argument("--remote-id", default=None, help=t("adopt.pair_remote_id_help"))
    pair.add_argument("--follow-from", default=None, help=t("adopt.pair_follow_from_help"))
    pair.add_argument("--confirm-backfill", type=int, default=None)
    pair.add_argument("--decline", action="store_true", help=t("adopt.pair_decline_help"))
    pair.set_defaults(handler=adopt_ops.run_pair, configure_logging=False)
    follow = steps.add_parser("set-follow-from", help=t("adopt.set_follow_from_help"),
                              allow_abbrev=False)  # fmt: skip
    follow.add_argument("series")
    follow.add_argument("chapter")
    follow.add_argument("--destination", dest="sub_destination", default=None)
    follow.set_defaults(handler=adopt_ops.run_set_follow_from, configure_logging=False)
    coverage = steps.add_parser("set-coverage", help=t("adopt.set_coverage_help"),
                                allow_abbrev=False)  # fmt: skip
    coverage.add_argument("series")
    coverage.add_argument("volume", help=t("adopt.coverage_volume_help"))
    coverage.add_argument("first", help=t("adopt.coverage_first_help"))
    coverage.add_argument("last", help=t("adopt.coverage_last_help"))
    coverage.add_argument("--destination", dest="sub_destination", default=None)
    coverage.set_defaults(handler=adopt_ops.run_set_coverage, configure_logging=False)


def open_session(resolved: ResolvedSettings, destination_id: str) -> AdoptionSession:
    """The adopter over the instance's database, library roots and the Kavita of the target."""
    from shelfhold.composition import default_network
    from shelfhold.export.targets.kavita.wiring import open_adoption
    from shelfhold.infrastructure.clock import SystemClock
    from shelfhold.integrations.apprise import operating_bus
    from shelfhold.ops.events import BusTargetEvents

    bus = operating_bus(SystemClock())  # the operating events reach the channels
    return open_adoption(
        resolved.values, destination_id, bus=bus, network=default_network(),
        events_for=lambda engine: BusTargetEvents(engine, bus),
    )  # fmt: skip


def _show(report: AdoptionReport) -> None:
    print(t("adopt.fingerprint_line", fingerprint=report.fingerprint))
    print(t("adopt.header", series=len(report.series), files=report.files))
    for series in report.series:
        if series.volume_only:
            print(t("adopt.volume_only_line", name=series.name))
        else:
            print(t("adopt.series_line", name=series.name, files=len(series.files)))
    for rename in report.renames:
        print(t("adopt.rename_line", from_path=rename.from_path, to_path=rename.to_path))
    for duplicate in report.duplicates:
        print(
            t(
                "adopt.duplicate_line",
                series=duplicate.series,
                number=duplicate.number,
                files=",".join(duplicate.files),
            )
        )


def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    from shelfhold.export.targets.kavita.adopt import AdoptionRefused

    if args.repatriate:
        return adopt_ops.run_repatriate(args, resolved)
    if args.pair_auto:
        return adopt_ops.run_pair_auto(args, resolved)
    if args.confirm_migration:
        return adopt_ops.run_confirm_migration(args, resolved)
    if not args.destination:
        print(t("adopt.needs_destination"), file=sys.stderr)
        return 2
    if not args.dry_run and not args.confirm:
        print(t("adopt.needs_confirmation"), file=sys.stderr)
        return 2
    session = open_session(resolved, args.destination)
    try:
        if args.dry_run:
            _show(session.preview(args.destination, args.library))
            return 0
        report = session.adopt(args.destination, confirm=args.confirm, library_ref=args.library)
    except PreviewStale:
        print(t("adopt.preview_stale"), file=sys.stderr)
        return STALE
    except AdoptionRefused as refused:
        print(t("adopt.refused", accounts=",".join(refused.accounts)), file=sys.stderr)
        return REFUSED
    _show(report)
    print(t("adopt.done", done=report.adopted_files, skipped=report.already_adopted))
    return 0
