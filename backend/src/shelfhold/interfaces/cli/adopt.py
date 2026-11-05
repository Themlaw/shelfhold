"""``shelfhold adopt``: adopt an existing adult Kavita library (EF-INT-014, EF-EXP-028).

`--dry-run` shows what adoption would do and writes nothing (not in the database, not on disk, not
in Kavita). Nothing is entered without `--confirm`. The existing files are referenced where they
are: never rewritten, moved, copied or deleted; the renames shown are proposals only.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.config import ResolvedSettings
from shelfhold.export.targets.kavita.adopt import AdoptionReport, AdoptionSession
from shelfhold.i18n import t

__all__ = ["AdoptionSession", "register", "run"]

REFUSED = 3


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    adopt = commands.add_parser("adopt", help=t("adopt.help"), allow_abbrev=False)
    adopt.add_argument("--dry-run", action="store_true", help=t("adopt.dry_run_help"))
    adopt.add_argument("--confirm", action="store_true", help=t("adopt.confirm_help"))
    adopt.add_argument("--destination", required=True, help=t("adopt.destination_help"))
    adopt.add_argument("--library", default=None, help=t("adopt.library_help"))
    adopt.set_defaults(handler=run)


def open_session(resolved: ResolvedSettings, destination_id: str) -> AdoptionSession:
    """The adopter over the instance's database, library roots and the Kavita of the target."""
    from shelfhold.export.targets.kavita.wiring import open_adoption

    return open_adoption(resolved.values, destination_id)


def _show(report: AdoptionReport) -> None:
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

    if not args.dry_run and not args.confirm:
        print(t("adopt.needs_confirmation"), file=sys.stderr)
        return 2
    session = open_session(resolved, args.destination)
    try:
        if args.dry_run:
            _show(session.preview(args.destination, args.library))
            return 0
        report = session.adopt(args.destination, confirmed=True, library_ref=args.library)
    except AdoptionRefused as refused:
        print(t("adopt.refused", accounts=",".join(refused.accounts)), file=sys.stderr)
        return REFUSED
    _show(report)
    print(t("adopt.done", done=report.adopted_files, skipped=report.already_adopted))
    return 0
