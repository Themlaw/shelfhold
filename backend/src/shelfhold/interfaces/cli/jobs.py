"""``shelfhold jobs list|retry|run``: the jobs that gave up, a file the server never indexed, a
periodic job to run now."""

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
    now = actions.add_parser("run", help=t("ops.help_jobs_run"), allow_abbrev=False)
    now.add_argument("kind")
    now.set_defaults(handler=run_now, configure_logging=False)
    retry = actions.add_parser("retry", help=t("ops.help_jobs_retry"), allow_abbrev=False)
    retry.add_argument("job_id")
    retry.set_defaults(handler=run_retry, configure_logging=False)
    cancel = actions.add_parser("cancel", help=t("ops.help_jobs_cancel"), allow_abbrev=False)
    cancel.add_argument("job_id")
    cancel.set_defaults(handler=run_cancel, configure_logging=False)
    replay = actions.add_parser("replay", help=t("ops.help_jobs_replay"), allow_abbrev=False)
    replay.add_argument("job_id")
    replay.set_defaults(handler=run_replay, configure_logging=False)
    priority = actions.add_parser("priority", help=t("ops.help_jobs_priority"), allow_abbrev=False)
    priority.add_argument("job_id")
    priority.add_argument("priority_class", help=t("ops.help_jobs_class"))
    priority.set_defaults(handler=run_priority, configure_logging=False)
    queue = commands.add_parser("queue", help=t("ops.help_queue"), allow_abbrev=False)
    steps = queue.add_subparsers(dest="queue_command", required=True)
    pause = steps.add_parser("pause", help=t("ops.help_queue_pause"), allow_abbrev=False)
    resume = steps.add_parser("resume", help=t("ops.help_queue_resume"), allow_abbrev=False)
    for leaf, handler in ((pause, run_pause), (resume, run_resume)):
        leaf.add_argument("--source", help=t("ops.help_queue_source"))
        leaf.set_defaults(handler=handler, configure_logging=False)


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


@ops_command
def run_now(args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call("ops.jobs_run", kind=args.kind)
    print(t("ops.done"))
    return 0


@ops_command
def run_cancel(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.job_cancel", job_id=args.job_id)
    if done["outcome"] == "cancelled":
        print(t("ops.job_cancelled", id=done["id"]))
    else:
        print(t("ops.job_cancelling", id=done["id"]))
    return 0


@ops_command
def run_replay(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.job_replay", job_id=args.job_id)
    if done["replayed"]:
        print(t("ops.job_replayed", id=done["id"]))
    else:
        print(t("ops.job_not_replayed", id=done["id"]))
    return 0


@ops_command
def run_priority(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.job_priority", job_id=args.job_id, priority_class=args.priority_class)
    print(t("ops.job_priority_set", id=done["id"], priority=done["priority_class"]))
    return 0


@ops_command
def run_pause(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.queue_pause", source=args.source)
    print(t("ops.queue_paused", scope=done["scope"], parked=done["parked"]))
    return 0


@ops_command
def run_resume(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.queue_resume", source=args.source)
    print(t("ops.queue_resumed", scope=done["scope"], resumed=done["resumed"]))
    return 0
