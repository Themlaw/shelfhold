"""``shelfhold contributor set-died-for-france ID (--yes | --no | --unknown)`` (EF-SRC-024, L15b).

Records whether a contributor died for France, which adds 30 years to the French term. Unknown is
the default and is read on the cautious side by the rights warning, which never blocks anything. A
reserved action: the host CLI or a fresh admin session, never a key.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

NEEDS_ONE_ANSWER = 2


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("contributor", help=t("ops.help_contributor"), allow_abbrev=False)
    actions = group.add_subparsers(dest="contributor_command", required=True)
    leaf = actions.add_parser(
        "set-died-for-france", help=t("ops.help_contributor_set_died_for_france"),
        allow_abbrev=False,
    )  # fmt: skip
    leaf.add_argument("contributor_id", help=t("ops.help_contributor_id"))
    leaf.add_argument("--yes", action="store_true", help=t("ops.help_died_yes"))
    leaf.add_argument("--no", action="store_true", help=t("ops.help_died_no"))
    leaf.add_argument("--unknown", action="store_true", help=t("ops.help_died_unknown"))
    leaf.set_defaults(handler=run_set_died_for_france, configure_logging=False)


@ops_command
def run_set_died_for_france(args: argparse.Namespace, channel: OpsChannel) -> int:
    chosen = [name for name in ("yes", "no", "unknown") if getattr(args, name)]
    if len(chosen) != 1:
        print(t("ops.choose_one_answer"), file=sys.stderr)
        return NEEDS_ONE_ANSWER
    done = channel.call(
        "ops.contributor_set_died_for_france", contributor=args.contributor_id, answer=chosen[0]
    )
    print(t("ops.died_for_france_set", contributor=done["contributor_id"], answer=done["answer"]))
    return 0
