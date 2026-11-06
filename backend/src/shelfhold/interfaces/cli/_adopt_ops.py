"""The commands of ``adopt`` and ``detach`` that go through the operating channel (L30, L31).

Pairing with the Suwayomi follow, the confirmation of the switch-over, the repatriation before the
unmount and the detachment. The server's socket answers when it runs; else the command takes the
instance lock itself (`_ops.ops_channel`). Nothing here deletes: the repatriation copies, the
detachment gives Suwayomi its settings back and keeps the files.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import fail, ops_command
from shelfhold.interfaces.local_cli import OpsChannel

NEEDS_CONFIRMATION = 2
REFUSED = 3


def _proposal_line(p: dict[str, Any]) -> str:
    best = p["candidates"][0] if p["candidates"] else None
    where = (
        t("pair.where", source=best["source_key"], remote=best["remote_id"], score=best["score"])
        if best
        else "-"
    )
    return t("pair.line", state=p["state"], id=p["export_series_id"], name=p["series"], where=where)


def _print_proposals(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        print(_proposal_line(row))
        if row["command"]:
            print(t("pair.command", command=row["command"]))


@ops_command
def run_pair(args: argparse.Namespace, channel: OpsChannel) -> int:
    destination = args.sub_destination or args.destination
    if args.series is None:
        _print_proposals(channel.call("ops.pair_propose", destination_id=destination)["proposals"])
        return 0
    if args.decline:
        channel.call("ops.pair_decline", export_series_id=args.series)
        print(t("pair.declined", id=args.series))
        return 0
    if not args.source_key or not args.remote_id:
        return fail(t("pair.needs_source"), NEEDS_CONFIRMATION)
    done = channel.call(
        "ops.pair_confirm", destination_id=destination, export_series_id=args.series,
        source_key=args.source_key, remote_id=args.remote_id, follow_from=args.follow_from,
        confirm_backfill=args.confirm_backfill,
    )  # fmt: skip
    if done["state"] == "coverage_unknown":
        print(t("pair.coverage_unknown", command=done["command"]), file=sys.stderr)
        return REFUSED
    print(t("pair.paired", id=done["export_series_id"], follow_from=done["follow_from"] or "-"))
    return 0


@ops_command
def run_pair_auto(args: argparse.Namespace, channel: OpsChannel) -> int:
    if args.confirm is None:
        report = channel.call(
            "ops.pair_auto", destination_id=args.destination, min_score=args.min_score
        )
        print(t("pair.fingerprint", fingerprint=report["fingerprint"]))
        print(t("pair.batch_header", ready=len(report["ready"]), held=len(report["held"])))
        _print_proposals(report["ready"] + report["held"])
        print(t("pair.batch_confirm_hint"))
        return 0
    done = channel.call(
        "ops.pair_auto", destination_id=args.destination, min_score=args.min_score,
        fingerprint=args.confirm,
    )  # fmt: skip
    for row in done["refused"]:
        print(t("pair.refused", id=row["export_series_id"], reason=row["reason"]), file=sys.stderr)
    for row in done["paired"]:
        print(t("pair.paired", id=row["export_series_id"], follow_from=row["follow_from"] or "-"))
    return REFUSED if done["refused"] else 0


@ops_command
def run_set_follow_from(args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call(
        "ops.adopt_set_follow_from", destination_id=args.sub_destination or args.destination,
        series=args.series, chapter=args.chapter,
    )  # fmt: skip
    print(t("ops.done"))
    return 0


@ops_command
def run_set_coverage(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call(
        "ops.adopt_set_coverage", destination_id=args.sub_destination or args.destination,
        series=args.series, volume=args.volume, first=args.first, last=args.last,
    )  # fmt: skip
    print(t("adopt.coverage_set", volume=args.volume, first=args.first, last=args.last,
            added=done["added"]))  # fmt: skip
    return 0


@ops_command
def run_confirm_migration(args: argparse.Namespace, channel: OpsChannel) -> int:
    if args.confirm is None:
        report = channel.call("ops.migration_report", destination_id=args.destination)
        print(t("migration.fingerprint", fingerprint=report["fingerprint"]))
        if not report["detected"]:
            print(t("migration.none_detected"))
            return 0
        for found in report["series"]:
            print(t("migration.series_line", id=found["export_series_id"],
                    dir=found["series_dir"], signals=",".join(found["signals"])))  # fmt: skip
        print(t("migration.steps"))
        for step in report["steps"]:
            print(t("migration.step", step=step))
        print(t("migration.confirm_hint"))
        return NEEDS_CONFIRMATION
    channel.call(
        "ops.adopt_confirm_migration", destination_id=args.destination, fingerprint=args.confirm
    )
    print(t("ops.done"))
    return 0


@ops_command
def run_repatriate(args: argparse.Namespace, channel: OpsChannel) -> int:
    if args.check:
        check = channel.call("ops.repatriate_check", destination_id=args.destination)
        return _report_check(check)
    if not args.dry_run and not args.confirm:
        print(t("repatriate.needs_dry_run"), file=sys.stderr)
        return NEEDS_CONFIRMATION
    if args.dry_run:
        plan = channel.call(
            "ops.repatriate_plan", destination_id=args.destination, source=args.source
        )
        print(t("repatriate.fingerprint", fingerprint=plan["fingerprint"]))
        print(
            t(
                "repatriate.header", series=plan["series"], chapters=plan["chapters"],
                files=plan["files"], size=plan["bytes_to_copy"], free=plan["free_bytes"],
            )
        )  # fmt: skip
        for problem in plan["problems"]:
            print(t("repatriate.problem", path=problem["path"], status=problem["status"]))
        print(t("repatriate.ready") if plan["ready"] else t("repatriate.not_ready"))
        return 0
    done = channel.call(
        "ops.repatriate_apply", destination_id=args.destination, fingerprint=args.confirm,
        source=args.source,
    )  # fmt: skip
    print(t("repatriate.done", copied=done["copied"], present=done["already_present"]))
    return _report_check(done["check"])


def _report_check(check: dict[str, Any]) -> int:
    """The files, then what Kavita says; the volume may go only if both agree."""
    for path in check["missing"] + check["different"]:
        print(t("repatriate.check_problem", path=path), file=sys.stderr)
    kavita = check["kavita"]
    if not kavita["verified"]:
        reasons = {
            "no_baseline": t("repatriate.kavita_no_baseline"),
            "unreachable": t("repatriate.kavita_unreachable"),
            "no_library": t("repatriate.kavita_no_library"),
            "baseline_incomplete": t("repatriate.kavita_baseline_incomplete"),
            "incomplete": t("repatriate.kavita_incomplete"),
        }
        print(reasons.get(kavita["reason"], t("repatriate.kavita_not_asked")), file=sys.stderr)
    else:
        print(t("repatriate.kavita_counts", before_series=kavita["series"][0],
                after_series=kavita["series"][1], before_files=kavita["files"][0],
                after_files=kavita["files"][1]))  # fmt: skip
        for problem in kavita["problems"]:
            print(t("repatriate.kavita_problem", kind=problem["kind"], what=problem["what"]),
                  file=sys.stderr)  # fmt: skip
    print(t("repatriate.safe") if check["safe_to_unmount"] else t("repatriate.unsafe"))
    return 0 if check["safe_to_unmount"] else REFUSED


@ops_command
def run_detach(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.detach_restore_upstream", shelf_id=args.shelf, edition_id=args.series)
    for row in done["series"]:
        print(t("detach.line", binding=row["binding_id"], source=row["restored_from"],
                ok=t("detach.ok") if row["ok"] else (row["error"] or "?")))  # fmt: skip
    for folder, files in done["managed"].items():
        print(t("detach.managed", folder=folder, count=len(files)))
    print(t("detach.files_kept"))
    return 0 if done["complete"] else REFUSED
