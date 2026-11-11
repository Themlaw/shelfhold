"""``shelfhold purge --now``: erase a Unit, or a whole Work, for good (EF-BIB-052, EF-BIB-053).

A reserved action: the host CLI only (never an API key). The tombstone is written first and made
durable, then the files go; what no purge can reach is said at the end. The identifier is typed
again as the confirmation, whether or not the Unit is irreplaceable.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

REFUSED = 3


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    purge = commands.add_parser("purge", help=t("purge.help"), allow_abbrev=False)
    purge.add_argument("--now", action="store_true", help=t("purge.now_help"))
    target = purge.add_mutually_exclusive_group(required=True)
    target.add_argument("--leftovers", action="store_true", help=t("purge.leftovers_help"))
    target.add_argument("--unit", default=None, help=t("purge.unit_help"))
    target.add_argument("--work", default=None, help=t("purge.work_help"))
    purge.add_argument("--confirm", default=None, help=t("purge.confirm_help"))
    purge.set_defaults(handler=run, configure_logging=False)


@ops_command
def _leftovers(args: argparse.Namespace, channel: OpsChannel) -> int:
    """The M1b migration's offer: what the purges of M1a left in the service. Without `--confirm`
    it only reads and prints the plan; with the version it printed, it lets the service go."""
    if args.confirm:
        done = channel.call("ops.leftovers_apply", if_match=args.confirm)
        print(
            t(
                "purge.leftovers_done",
                released=done["released"],
                forgotten=done["forgotten"],
                unfollowed=done.get("unfollowed", 0),
            )
        )
        return 0 if not done["unsettled"] else REFUSED
    plan = channel.call("ops.leftovers_plan")
    if not plan["connected"]:
        print(t("purge.leftovers_not_connected"))
        return 0
    series = len(plan.get("series", []))
    print(
        t(
            "purge.leftovers_plan",
            downloads=len(plan["downloads"]),
            dead=len(plan["dead_tickets"]),
            series=series,
        )
    )
    if plan.get("series_unknown"):
        print(t("purge.leftovers_series_unknown"), file=sys.stderr)
    if plan["downloads"] or plan["dead_tickets"] or series:
        print(t("purge.leftovers_confirm", version=plan["version"]))
    return 0


@ops_command
def _purge(args: argparse.Namespace, channel: OpsChannel) -> int:
    kind, anchor = ("unit", args.unit) if args.unit else ("work", args.work)
    report = channel.call("ops.purge", kind=kind, anchor_id=anchor, typed=args.confirm)
    print(t("purge.done", units=len(report["units"]), erased=report["erased"]))
    for root in report["unreachable_roots"] + report["pending_roots"]:
        print(t("purge.root_pending", root=root), file=sys.stderr)
    for alert in report["alerts"]:
        print(t("purge.alert", alert=alert), file=sys.stderr)
    released = report.get("upstream_released", 0)
    kept = report.get("upstream_kept_by_others", 0)
    unfollowed = report.get("unfollowed", 0)
    if released or kept or unfollowed:
        print(t("purge.upstream", released=released, kept=kept, unfollowed=unfollowed))
    for backup in report.get("retained_backups", []):
        print(t("purge.retained_backup", name=backup["name"]), file=sys.stderr)
    print(t("purge.out_of_reach"))
    for item in report["out_of_reach"]:
        print(t("purge.out_of_reach_item", item=item))
    return 0 if report["complete"] else REFUSED


def run(args: argparse.Namespace, resolved: object) -> int:
    if args.leftovers:  # a plan first (read only), then the same command with its version
        return _leftovers(args, resolved)  # type: ignore[arg-type]
    if not args.now:
        print(t("purge.needs_now"), file=sys.stderr)
        return 2
    if not args.confirm:
        print(t("purge.needs_confirmation"), file=sys.stderr)
        return 2
    return _purge(args, resolved)  # type: ignore[arg-type]
