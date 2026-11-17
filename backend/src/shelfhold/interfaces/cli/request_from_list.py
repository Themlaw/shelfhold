"""``shelfhold request --from-list <file>`` (EF-USR-020, M4 L31).

Imports a list of wanted books written as plain text, one title per line (an ISBN if there is one):
a reading shelf for the profile and a request for every line the library does not hold. Nothing is
searched in a source. The file is read by this command, at most 64 KiB, and read again by the
server as untrusted text. Without `--for-profile` the list is the instance admin's; with it, the
list is made for that child, which is a parental decision (a reserved action): the host's command
line is allowed it, and an age is still chosen at approval of each of her requests.

The admin reads and approves the whole list on one screen: the command prints its address.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shelfhold.acquisition.domain.request_list import MAX_LIST_BYTES
from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import fail, ops_command
from shelfhold.interfaces.local_cli import OpsChannel

REFUSED = 2


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    request = commands.add_parser("request", help=t("ops.help_request"), allow_abbrev=False)
    request.add_argument(
        "--from-list", dest="from_list", required=True, metavar="FILE",
        help=t("ops.help_request_from_list"),
    )  # fmt: skip
    request.add_argument("--for-profile", dest="profile", help=t("ops.help_request_for_profile"))
    request.add_argument("--name", help=t("ops.help_request_name"))
    request.set_defaults(handler=run_request, configure_logging=False)


def _read(source: str) -> bytes | None:
    """The bytes of the list, at most the bound plus one (so that a list over it is refused, never
    cut); None when it cannot be read."""
    try:
        if source == "-":
            return sys.stdin.read(MAX_LIST_BYTES + 1).encode("utf-8")
        with Path(source).open("rb") as handle:
            return handle.read(MAX_LIST_BYTES + 1)
    except OSError:
        return None


def run_request(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    raw = _read(args.from_list)
    if raw is None:
        return fail(t("ops.request_list_refused", reason="unreadable"), REFUSED)
    if len(raw) > MAX_LIST_BYTES:
        return fail(t("ops.request_list_refused", reason="too_large"), REFUSED)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return fail(t("ops.request_list_refused", reason="not_text"), REFUSED)
    name = args.name or (None if args.from_list == "-" else Path(args.from_list).stem)

    @ops_command
    def send(args: argparse.Namespace, channel: OpsChannel) -> int:
        method = "ops.request_from_list_for_profile" if args.profile else "ops.request_from_list"
        answer = channel.call(method, text=text, name=name, profile=args.profile)
        print(
            t(
                "ops.request_list_done",
                in_library=answer["in_library"],
                requested=answer["requested"],
            )
        )
        if answer["annotated"]:
            print(t("ops.request_list_annotated", count=answer["annotated"]))
        print(t("ops.request_list_review", id=answer["id"]))
        return 0

    return send(args, resolved)
