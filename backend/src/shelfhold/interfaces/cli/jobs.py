"""``shelfhold jobs list|retry``: the jobs that gave up, and a file the server never indexed."""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("jobs", help=t("ops.help_jobs"), allow_abbrev=False)
    actions = group.add_subparsers(dest="jobs_command", required=True)
    listing = actions.add_parser("list", help=t("ops.help_jobs_list"), allow_abbrev=False)
    listing.add_argument("--status", help=t("ops.help_jobs_status"))
    listing.set_defaults(handler=run_list, configure_logging=False)
    retry = actions.add_parser("retry", help=t("ops.help_jobs_retry"), allow_abbrev=False)
    retry.add_argument("job_id")
    retry.set_defaults(handler=run_retry, configure_logging=False)


@ops_command
def run_list(args: argparse.Namespace, channel: OpsChannel) -> int:
    for job in channel.call("ops.jobs_list", status=args.status)["items"]:
        print(
            t("ops.job_line", id=job["id"], kind=job["kind"], status=job["status"],
              code=job["error_code"] or "-", attempts=job["attempts"])
        )  # fmt: skip
    return 0


@ops_command
def run_retry(args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call("ops.jobs_retry", job_id=args.job_id)
    print(t("ops.done"))
    return 0
