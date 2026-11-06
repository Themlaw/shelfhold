"""``shelfhold backfill --gaps|--all <edition>``: the estimate first, a confirmation above 100
Units or 5 GiB (EF-EXP-053).

The estimate (Units, size, free space) is always shown. Up to the thresholds the backfill starts
at once; above them it starts only when `--confirm-units` and `--confirm-bytes` repeat the figures
that were shown (exit code 3 otherwise, with the exact command to confirm). `--dry-run` shows the
estimate and starts nothing. Starting does not download by itself: the follow cycle brings the Units
back within its per-cycle caps, after the new chapters.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

NEEDS_CONFIRMATION = 3


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backfill = commands.add_parser("backfill", help=t("ops.help_backfill"), allow_abbrev=False)
    backfill.add_argument("edition_id", help=t("ops.help_backfill_edition"))
    mode = backfill.add_mutually_exclusive_group()
    mode.add_argument("--gaps", action="store_true", help=t("ops.help_backfill_gaps"))
    mode.add_argument("--all", action="store_true", help=t("ops.help_backfill_all"))
    backfill.add_argument("--dry-run", action="store_true", help=t("ops.help_backfill_dry_run"))
    backfill.add_argument(
        "--confirm-units", type=int, default=None, help=t("ops.help_backfill_confirm_units")
    )
    backfill.add_argument(
        "--confirm-bytes", type=int, default=None, help=t("ops.help_backfill_confirm_bytes")
    )
    backfill.set_defaults(handler=run, configure_logging=False)


def _show(figures: dict[str, object]) -> None:
    print(
        t("ops.backfill_estimate", mode=figures["mode"], units=figures["units"],
          size=figures["size_bytes"], free=figures["free_bytes"])
    )  # fmt: skip


@ops_command
def _backfill(args: argparse.Namespace, channel: OpsChannel) -> int:
    mode = "all" if args.all else "gaps"
    if args.dry_run:
        _show(channel.call("ops.backfill_estimate", edition_id=args.edition_id, mode=mode))
        return 0
    answer = channel.call(
        "ops.backfill_start", edition_id=args.edition_id, mode=mode,
        confirm_units=args.confirm_units, confirm_bytes=args.confirm_bytes,
    )  # fmt: skip
    _show(answer)
    if not answer["started"]:
        print(
            t("ops.backfill_needs_confirmation", mode=mode, edition=args.edition_id,
              units=answer["units"], size=answer["size_bytes"]),
            file=sys.stderr,
        )  # fmt: skip
        return NEEDS_CONFIRMATION
    print(t("ops.backfill_started"))
    return 0


def run(args: argparse.Namespace, resolved: object) -> int:
    if not (args.gaps or args.all):
        print(t("ops.backfill_choose_mode"), file=sys.stderr)
        return 2
    if (args.confirm_units is None) != (args.confirm_bytes is None):
        print(t("ops.backfill_confirm_pair"), file=sys.stderr)
        return 2
    return _backfill(args, resolved)  # type: ignore[arg-type]
