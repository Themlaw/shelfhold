"""``shelfhold plugin-runner``: the companion that runs isolated plugins (D-P1, EF-SRC-016).

Started by its own container (same image, uid 1001, no network, no `/data`), never by hand and never
by the core. It reads no settings and no database: there is nothing of the core where it runs. It
checks its sandbox and refuses to start (exit 3, nothing bound) when it is not one; ``--check`` only
does that check.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from shelfhold.i18n import t
from shelfhold.plugin_host.adapters.rpc.runner_socket import DEFAULT_SOCKET
from shelfhold.plugin_host.runner.selfcheck import SelfCheckFailed, self_check
from shelfhold.plugin_host.runner.server import RunnerConfig, configure_logging, serve

__all__ = ["REFUSED", "register", "run"]

REFUSED = 3
DEFAULT_PLUGINS = "/plugins"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("plugin-runner", help=t("cli.plugin_runner_help"))
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help=t("cli.plugin_runner_socket_help"))
    parser.add_argument(
        "--plugins", default=DEFAULT_PLUGINS, help=t("cli.plugin_runner_plugins_help")
    )
    parser.add_argument("--check", action="store_true", help=t("cli.plugin_runner_check_help"))
    parser.set_defaults(handler=run, standalone=True)


def run(args: argparse.Namespace, resolved: object = None) -> int:
    """`resolved` is never given: this command runs before any setting is read."""
    configure_logging()
    try:
        seen = self_check()
    except SelfCheckFailed as exc:
        print(t("plugin.runner_refused", problems=str(exc)), file=sys.stderr)
        return REFUSED
    print(t("plugin.runner_ready", uid=seen.uid), file=sys.stderr)
    if args.check:
        return 0
    config = RunnerConfig(socket_path=Path(args.socket), plugins_root=Path(args.plugins))
    try:
        asyncio.run(serve(config))
    except SelfCheckFailed as exc:  # it changed between the two looks
        print(t("plugin.runner_refused", problems=str(exc)), file=sys.stderr)
        return REFUSED
    return 0
