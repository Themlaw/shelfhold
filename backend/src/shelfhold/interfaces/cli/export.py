"""``shelfhold export --dry-run``: show the plan of a destination, write nothing (EF-INT-014).

The application of a plan needs the running instance (it holds the instance lock and the leases
of the roots): it goes through the API (`POST /api/v1/plans/{id}/apply`) or the local socket of
L23, never through a second process. `export` without `--dry-run` says so and stops.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

if TYPE_CHECKING:
    from shelfhold.export.application.pipeline import ExportPipeline


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    export = commands.add_parser("export", help=t("cli.export_help"), allow_abbrev=False)
    export.add_argument("--dry-run", action="store_true", help=t("cli.export_dry_run_help"))
    export.add_argument("--destination", default=None, help=t("cli.export_destination_help"))
    export.set_defaults(handler=run)
    kinds = export.add_subparsers(dest="export_command", required=False)
    item = kinds.add_parser("item", help=t("cli.export_item_help"), allow_abbrev=False)
    item_actions = item.add_subparsers(dest="item_command", required=True)
    show = item_actions.add_parser("show", help=t("cli.export_item_show_help"), allow_abbrev=False)
    show.add_argument("item_id", help=t("cli.export_item_id_help"))
    show.set_defaults(handler=run_item_show, configure_logging=False)


def open_pipeline(resolved: ResolvedSettings) -> ExportPipeline:
    """The pipeline over the instance's database and library roots, for planning only."""
    from shelfhold.export.application.wiring import open_pipeline as build

    return build(resolved.values)


@ops_command
def run_item_show(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.export_item_show", item=args.item_id)
    print(t("export.item_header", id=done["id"], kind=done["kind"], edition=done["edition_id"]))
    if done["label"]:
        print(t("export.item_volume", label=done["label"]))
    print(t("export.item_state", units=done["units"],
            state=t("export.item_closed" if done["closed"] else "export.item_open")))  # fmt: skip
    if done["kept"]:
        print(t("export.item_kept", path=done["kept"]))
    else:
        print(t("export.item_kept_problem", code=done["kept_problem"]))
    return 0


def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    if args.destination is None:
        print(t("export.destination_required"), file=sys.stderr)
        return 2
    if not args.dry_run:
        print(t("export.apply_needs_the_instance"), file=sys.stderr)
        return 2
    preview = open_pipeline(resolved).preview(args.destination)
    print(
        t(
            "export.plan_header",
            plan=preview.plan_id,
            ops=len(preview.ops),
            blocked=len(preview.blocked),
        )
    )
    for op in preview.ops:
        print(t("export.op_line", kind=op.kind, path=op.rel_path or "-"))
    for blocked in preview.blocked:
        print(
            t("export.blocked_line", code=blocked.code, unit=blocked.unit_id or "-"),
            file=sys.stderr,
        )
    for warning in preview.warnings:
        print(t("export.warning_line", code=warning.code, unit=warning.unit_id or "-"))
    return 0
