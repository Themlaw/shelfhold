"""``shelfhold source add suwayomi`` (EF-INT-024, EF-SRC-031).

The authentication mode is read from the server; a mode this release cannot use is refused.
The secret (``user:password``) is read from a file or stdin, never from argv.
"""

from __future__ import annotations

import argparse

from shelfhold.i18n import t
from shelfhold.interfaces.cli._headless import fail, headless_command, read_secret
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import HeadlessCommands, OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("source", help=t("cli.source_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="source_command", required=True)
    add = actions.add_parser("add", help=t("cli.source_add_help"), allow_abbrev=False)
    kinds = add.add_subparsers(dest="source_kind", required=True)
    suwayomi = kinds.add_parser("suwayomi", help=t("cli.source_add_suwayomi_help"),
                                allow_abbrev=False)  # fmt: skip
    suwayomi.add_argument("--url", required=True, help=t("cli.source_url_help"))
    suwayomi.add_argument("--secret-file", help=t("cli.source_secret_file_help"))
    suwayomi.set_defaults(handler=run_add_suwayomi, configure_logging=False)
    # the exits of EF-INT-023
    secret = actions.add_parser("set-secret", help=t("ops.help_source_set_secret"),
                                allow_abbrev=False)  # fmt: skip
    secret.add_argument("--secret-file", required=True, help=t("cli.source_secret_file_help"))
    secret.set_defaults(handler=run_set_secret, configure_logging=False)
    recheck = actions.add_parser("recheck", help=t("ops.help_source_recheck"), allow_abbrev=False)
    recheck.set_defaults(handler=run_recheck, configure_logging=False)
    reconnect = actions.add_parser("reconnect", help=t("ops.help_source_reconnect"),
                                   allow_abbrev=False)  # fmt: skip
    reconnect.set_defaults(handler=run_reconnect, configure_logging=False)


@headless_command
def run_add_suwayomi(args: argparse.Namespace, commands: HeadlessCommands) -> int:
    secret: str | None = None
    if args.secret_file:
        try:
            secret = read_secret(args.secret_file)
        except (OSError, UnicodeDecodeError) as error:
            return fail(t("headless.secret_unreadable", detail=error))
    added = commands.source_add_suwayomi(url=args.url, secret=secret)
    if added.created:
        print(t("headless.source_added", mode=added.auth_mode))
    else:
        print(t("headless.source_updated", mode=added.auth_mode))
    return 0


@ops_command
def run_set_secret(args: argparse.Namespace, channel: OpsChannel) -> int:
    try:
        secret = read_secret(args.secret_file)
    except (OSError, UnicodeDecodeError) as error:
        return fail(t("headless.secret_unreadable", detail=error))
    channel.call("ops.source_set_secret", secret=secret)
    print(t("ops.done"))
    return 0


@ops_command
def run_recheck(_args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call("ops.source_recheck")
    print(t("ops.done"))
    return 0


@ops_command
def run_reconnect(_args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call("ops.source_reconnect")
    print(t("ops.done"))
    return 0
