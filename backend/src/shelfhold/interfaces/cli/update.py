"""``shelfhold update check``: is a newer version out? (ENF-GOV-004, ADR-0029, ADR-0030).

Local CLI, a read: it changes nothing and installs nothing. Off by default: without
`SHELFHOLD_UPDATE_CHECK_URL` it refuses, with a clear message, and makes no connection. With it, one
guarded GET (SSRF guard, size bound while reading, a User-Agent that carries no version) and the
answer in one line. A failure is a message and the exit code 1; the server never runs this.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from shelfhold import __version__
from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.platform.update_check import (
    UpdateStatus,
    check_for_update,
    update_check_enabled,
    update_fetcher,
)

OFF = 2


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("update", help=t("cli.update_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="update_action", required=True)
    check = actions.add_parser("check", help=t("cli.update_check_help"), allow_abbrev=False)
    check.set_defaults(handler=run_check, configure_logging=False)


async def _ask(resolved: ResolvedSettings) -> UpdateStatus:
    from shelfhold.composition import default_network

    fetcher = update_fetcher(default_network())
    try:
        return await check_for_update(
            fetcher, resolved.values.update_check_url, current=__version__
        )
    finally:
        await fetcher.aclose()


def run_check(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    if not update_check_enabled(resolved.values):
        print(t("update.off"), file=sys.stderr)
        return OFF
    status = asyncio.run(_ask(resolved))
    if status.state == "ok" and status.newer:
        print(t("update.newer", latest=status.latest))
        return 0
    if status.state == "ok":
        print(t("update.current", latest=status.latest))
        return 0
    print(t("update.failed"), file=sys.stderr)
    return 1
