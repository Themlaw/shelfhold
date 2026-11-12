"""``shelfhold maintenance on|off|status`` (EF-INT-018, EF-INT-014).

While the mode is on, nothing writes: no acquisition, no export, no scheduled job (they wait and are
caught up once after the exit); reading and `/healthz` stay open. `on` and `off` are reserved
actions: the host CLI or a fresh admin session, never a key. `off` is the exit of the waiting state
`maintenance` that `status` lists for as long as the mode lasts.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

SECONDS_PER_HOUR = 3600


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("maintenance", help=t("ops.help_maintenance"), allow_abbrev=False)
    actions = group.add_subparsers(dest="maintenance_command", required=True)
    on = actions.add_parser("on", help=t("ops.help_maintenance_on"), allow_abbrev=False)
    on.set_defaults(handler=run_on, configure_logging=False)
    off = actions.add_parser("off", help=t("ops.help_maintenance_off"), allow_abbrev=False)
    off.set_defaults(handler=run_off, configure_logging=False)
    status = actions.add_parser("status", help=t("ops.help_maintenance_status"), allow_abbrev=False)
    status.set_defaults(handler=run_status, configure_logging=False)


@ops_command
def run_on(_args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.maintenance_on")
    if done["changed"]:
        print(t("ops.maintenance_entered", running=done["running_jobs"]))
    else:
        print(t("ops.maintenance_already"))
    return 0


@ops_command
def run_off(_args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.maintenance_off")
    if done["changed"]:
        print(t("ops.maintenance_left"))
    else:
        print(t("ops.maintenance_not_active"))
    return 0


@ops_command
def run_status(_args: argparse.Namespace, channel: OpsChannel) -> int:
    state = channel.call("ops.maintenance_status")
    mode = state["maintenance"]
    if mode["active"]:
        print(t("ops.maintenance_state_on", since=mode["since"], running=mode["running_jobs"]))
    else:
        print(t("ops.maintenance_state_off"))
    window = state["window"]
    following = window["next"]
    print(
        t(
            "ops.maintenance_window_line", timezone=window["timezone"], start=window["start"],
            end=window["end"], start_utc=following["start_utc"], end_utc=following["end_utc"],
            start_local=following["start_local"], end_local=following["end_local"],
            hours=round(following["duration_s"] / SECONDS_PER_HOUR, 2),
        )
    )  # fmt: skip
    return 0
