"""``shelfhold target add kavita`` (EF-INT-024, ENF-SEC-013).

The Kavita key is the key of an administrator account: the command stops on that warning until
``--accept-admin-risk``. The account's password is read from a file or stdin, never from argv.
"""

from __future__ import annotations

import argparse
import json
import sys

from shelfhold.config import ResolvedSettings
from shelfhold.headless import AdminRiskNotAccepted
from shelfhold.i18n import t
from shelfhold.interfaces.cli._detach_ops import run_remove
from shelfhold.interfaces.cli._headless import (
    fail,
    headless_command,
    read_secret,
)
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import HeadlessCommands, OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("target", help=t("cli.target_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="target_command", required=True)
    add = actions.add_parser("add", help=t("cli.target_add_help"), allow_abbrev=False)
    kinds = add.add_subparsers(dest="target_kind", required=True)
    kavita = kinds.add_parser("kavita", help=t("cli.target_add_kavita_help"), allow_abbrev=False)
    kavita.add_argument("--url", required=True, help=t("cli.target_url_help"))
    kavita.add_argument("--user", required=True, help=t("cli.target_user_help"))
    kavita.add_argument("--password-file", required=True, help=t("cli.target_password_file_help"))
    kavita.add_argument("--name", default="kavita", help=t("cli.target_name_help"))
    kavita.add_argument("--path-map", help=t("cli.target_path_map_help"))
    kavita.add_argument("--accept-admin-risk", action="store_true",
                        help=t("cli.target_accept_risk_help"))  # fmt: skip
    kavita.set_defaults(handler=run_add_kavita, configure_logging=False)
    # the exits of EF-INT-023
    for name, handler, help_text in (
        ("recheck", run_recheck, t("ops.help_target_recheck")),
        ("reconnect", run_reconnect, t("ops.help_target_reconnect")),
    ):
        leaf = actions.add_parser(name, help=help_text, allow_abbrev=False)
        leaf.add_argument("target_id")
        leaf.set_defaults(handler=handler, configure_logging=False)
    remove = actions.add_parser("remove", help=t("removal.help"), allow_abbrev=False)
    remove.add_argument("destination_id", help=t("removal.destination_help"))
    mode = remove.add_mutually_exclusive_group(required=True)
    mode.add_argument("--keep-files", action="store_true", help=t("removal.keep_help"))
    mode.add_argument("--delete-files", action="store_true", help=t("removal.delete_help"))
    remove.add_argument("--confirm", default=None, help=t("removal.confirm_help"))
    remove.add_argument("--give-up", action="store_true", help=t("removal.give_up_help"))
    remove.set_defaults(handler=run_remove, configure_logging=False)
    rotate = actions.add_parser("rotate-key", help=t("ops.help_target_rotate_key"),
                                allow_abbrev=False)  # fmt: skip
    rotate.add_argument("target_id")
    rotate.add_argument("--password-file", required=True, help=t("cli.target_password_file_help"))
    rotate.set_defaults(handler=run_rotate_key, configure_logging=False)


def run_add_kavita(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    if not args.accept_admin_risk:  # stop before the channel: nothing is read, sent or stored
        print(t("headless.admin_risk_warning"), file=sys.stderr)
        return 1
    return _add(args, resolved)


@headless_command
def _add(args: argparse.Namespace, commands: HeadlessCommands) -> int:
    try:
        password = read_secret(args.password_file)
    except (OSError, UnicodeDecodeError) as error:
        return fail(t("headless.secret_unreadable", detail=error))
    try:
        path_map = json.loads(args.path_map) if args.path_map else None
    except ValueError:
        return fail(t("headless.path_map_invalid"))
    try:
        added = commands.target_add_kavita(
            name=args.name, url=args.url, username=args.user, password=password,
            accept_admin_risk=True, path_map=path_map,
        )  # fmt: skip
    except AdminRiskNotAccepted:  # unreachable here, the flag is checked above
        return fail(t("headless.admin_risk_warning"))
    if added.created:
        print(t("headless.target_added", name=added.name))
    else:
        print(t("headless.target_exists", name=added.name))
    if added.key_expires_at is not None:
        print(t("headless.key_expires", date=added.key_expires_at.date().isoformat()))
    return 0


@ops_command
def run_recheck(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.target_recheck", target_id=args.target_id)
    for series in answer["cleared"]:
        print(t("ops.series_cleared", series=series))
    for series in answer["denied"]:
        print(t("ops.series_denied", series=series), file=sys.stderr)
    return 1 if answer["denied"] else 0


@ops_command
def run_reconnect(args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call("ops.target_reconnect", target_id=args.target_id)
    print(t("ops.done"))
    return 0


@ops_command
def run_rotate_key(args: argparse.Namespace, channel: OpsChannel) -> int:
    try:
        password = read_secret(args.password_file)
    except (OSError, UnicodeDecodeError) as error:
        return fail(t("headless.secret_unreadable", detail=error))
    channel.call("ops.target_rotate_key", target_id=args.target_id, password=password)
    print(t("ops.key_rotated"))
    return 0
