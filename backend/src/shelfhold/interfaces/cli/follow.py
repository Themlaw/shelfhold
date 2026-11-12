"""``shelfhold follow``, ``shelfhold follow gap-ack`` and ``shelfhold unfollow`` (EF-SRC-029,
EF-INT-014, EF-BIB-056).

`follow` makes one entry in the connected service's library, in the `Shelfhold` category, without
automatic download; asking twice changes nothing. `unfollow` keeps the series in the service; with
`--remove-upstream` it leaves the library too, and that needs `--confirm` (exit code 3 otherwise,
with the exact command). The route and this command go through the same service.
`follow gap-ack <edition> <number>` says that a number the series shows as missing does not exist:
the gap is no longer told. `--undo` takes it back.
`follow check <edition>` asks for a look at one series at once (EF-SRC-038): a job of the news
class, spaced by upstream source like any look.
`follow export [--format opml|csv]` writes the followed series as plain text
(EF-INT-027): read-only, straight from the database, no running instance needed.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.acquisition.application.follows_export import FORMATS, export_follows
from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

NEEDS_CONFIRMATION = 3
GAP_ACK = "gap-ack"
CHECK = "check"
EXPORT = "export"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    follow = commands.add_parser("follow", help=t("ops.help_follow_series"), allow_abbrev=False)
    _series(follow, remote_id_required=False)  # `follow export` has no series
    follow.add_argument("number", nargs="?", help=t("ops.help_gap_ack_number"))
    follow.add_argument("--track", default="main", help=t("ops.help_gap_ack_track"))
    follow.add_argument("--segment", default="", help=t("ops.help_gap_ack_segment"))
    follow.add_argument("--undo", action="store_true", help=t("ops.help_gap_ack_undo"))
    follow.add_argument(
        "--format", choices=FORMATS, default="opml", help=t("ops.help_export_format")
    )
    follow.set_defaults(handler=run_follow, configure_logging=False)
    unfollow = commands.add_parser(
        "unfollow", help=t("ops.help_unfollow_series"), allow_abbrev=False
    )
    _series(unfollow)
    unfollow.add_argument(
        "--remove-upstream", action="store_true", help=t("ops.help_unfollow_remove_upstream")
    )
    unfollow.add_argument("--confirm", action="store_true", help=t("ops.help_unfollow_confirm"))
    unfollow.set_defaults(handler=run_unfollow, configure_logging=False)


def _series(parser: argparse.ArgumentParser, *, remote_id_required: bool = True) -> None:
    parser.add_argument("source_key", help=t("ops.help_follow_source_key"))
    parser.add_argument(
        "remote_id", nargs=None if remote_id_required else "?", help=t("ops.help_follow_remote_id")
    )


@ops_command
def _gap_ack(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call(
        "ops.follow_gap_ack", edition_id=args.remote_id, number=args.number, track=args.track,
        segment=args.segment, undo=args.undo,
    )  # fmt: skip
    if args.undo:
        print(t("ops.gap_ack_undone") if answer["changed"] else t("ops.gap_ack_nothing_to_undo"))
    else:
        print(t("ops.gap_ack_done") if answer["changed"] else t("ops.gap_ack_already"))
    return 0


@ops_command
def _check(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.follow_check", edition_id=args.remote_id)
    print(answer["message"])
    return 0


def _export(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.migrate import current_revision, head_revision

    database = resolved.values.database_path
    if not database.exists() or current_revision(database) != head_revision():
        print(t("status.database_not_migrated"), file=sys.stderr)
        return 1
    engine = create_db_engine(database)
    try:
        content = export_follows(engine, args.format)
    finally:
        engine.dispose()
    print(content, end="")
    return 0


@ops_command
def _follow(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.follow", source_key=args.source_key, remote_id=args.remote_id)
    if answer["state"] == "in_progress":
        print(t("ops.follow_in_progress"))
    else:
        print(t("ops.follow_done") if answer["sent"] else t("ops.follow_already"))
    return 0


def run_follow(args: argparse.Namespace, resolved: object) -> int:
    """`follow gap-ack <edition> <number>` shares the command with `follow <source> <series>`:
    no source key is called `gap-ack`."""
    if args.source_key == EXPORT:
        if args.number is not None or args.remote_id is not None:
            print(t("ops.follow_extra_argument"), file=sys.stderr)
            return 2
        return _export(args, resolved)  # type: ignore[arg-type]
    if args.remote_id is None:  # optional for `export` only
        print(t("ops.follow_needs_remote_id"), file=sys.stderr)
        return 2
    if args.source_key == GAP_ACK:
        if args.number is None:
            print(t("ops.gap_ack_needs_number"), file=sys.stderr)
            return 2
        return _gap_ack(args, resolved)  # type: ignore[arg-type]
    if args.source_key == CHECK:
        if args.number is not None:
            print(t("ops.follow_extra_argument"), file=sys.stderr)
            return 2
        return _check(args, resolved)  # type: ignore[arg-type]
    if args.number is not None:
        print(t("ops.follow_extra_argument"), file=sys.stderr)
        return 2
    return _follow(args, resolved)  # type: ignore[arg-type]


@ops_command
def _unfollow(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call(
        "ops.unfollow", source_key=args.source_key, remote_id=args.remote_id,
        remove_upstream=args.remove_upstream, confirm=args.confirm,
    )  # fmt: skip
    print(t("ops.unfollow_removed") if answer["removed"] else t("ops.unfollow_done"))
    return 0


def run_unfollow(args: argparse.Namespace, resolved: object) -> int:
    if args.remove_upstream and not args.confirm:
        print(
            t("ops.unfollow_needs_confirmation", source_key=args.source_key,
              remote_id=args.remote_id),
            file=sys.stderr,
        )  # fmt: skip
        return NEEDS_CONFIRMATION
    return _unfollow(args, resolved)  # type: ignore[arg-type]
