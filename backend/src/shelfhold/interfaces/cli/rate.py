"""``shelfhold rate AGE (--work ID | --edition ID | --pending [--confirm TOKEN])`` (EF-INT-014).

The exit of `rating_pending` and `follow_suspended` (EF-INT-023, EF-EXP-048). Rating is a reserved
action: the host's console or a fresh admin session holding the right to classify, never a key.
Raising an age prints the plan of the files that no longer fit a shelf below 18+ and withdraws
none: that is `shelf withdraw`, with its own confirmation. `--pending` ("Classer tout en lot")
lists the Works first and rates them only for the confirmation the list gives back, typed again.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel

EXIT_NEEDS_CONFIRMATION = 2


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    rate = commands.add_parser("rate", help=t("rate.help"), allow_abbrev=False)
    rate.add_argument("age", help=t("rate.age_help"))
    rate.add_argument("--work", default=None, help=t("rate.work_help"))
    rate.add_argument("--edition", default=None, help=t("rate.edition_help"))
    rate.add_argument("--pending", action="store_true", help=t("rate.pending_help"))
    rate.add_argument("--confirm", default=None, help=t("rate.confirm_help"))
    rate.set_defaults(handler=run, configure_logging=False)


def _suggestions(suggestions: list[dict[str, object]]) -> None:
    """The suggested ages that rating a Work leaves open: each keeps its Edition out until
    `shelfhold rate AGE --edition ID`."""
    if not suggestions:
        return
    print(t("rate.suggestions_open", count=len(suggestions)), file=sys.stderr)
    for s in suggestions:
        age = "X" if s["x"] else f"{s['age']}+"
        print(
            t("rate.suggestion_line", edition=s["edition_id"], age=age, provenance=s["provenance"]),
            file=sys.stderr,
        )


def _withdrawals(withdrawals: list[dict[str, str]]) -> None:
    if not withdrawals:
        return
    print(t("rate.withdrawals_plan", count=len(withdrawals)), file=sys.stderr)
    for w in withdrawals:
        print(
            t("rate.withdrawal_line", edition=w["edition_id"], shelf=w["shelf_id"],
              reason=w["reason"]),
            file=sys.stderr,
        )  # fmt: skip


@ops_command
def run(args: argparse.Namespace, channel: OpsChannel) -> int:
    chosen = [bool(args.work), bool(args.edition), bool(args.pending)]
    if chosen.count(True) != 1 or (args.confirm and not args.pending):
        print(t("rate.choose_one"), file=sys.stderr)
        return EXIT_NEEDS_CONFIRMATION
    if args.pending:
        return _batch(args, channel)
    done = channel.call("ops.rate", age=args.age, work_id=args.work, edition_id=args.edition)
    print(t("rate.done", target=done["target"], age=args.age))
    _withdrawals(done["withdrawals"])
    _suggestions(done["open_suggestions"])
    return 0


def _batch(args: argparse.Namespace, channel: OpsChannel) -> int:
    listed = channel.call("ops.rate_pending", age=args.age)
    items = [i for i in listed["items"] if i["state"] != "suggestion_above"]
    for waiting in listed["items"]:  # rated Works whose Edition a higher suggestion keeps out
        if waiting["state"] == "suggestion_above":
            _suggestions(waiting["suggestions"])
    if not items:
        print(t("rate.pending_none"))
        return 0
    if args.confirm != listed["token"]:
        print(t("rate.pending_header", count=len(items), age=listed["age"]))
        for item in items:
            print(t("rate.pending_line", work=item["work_id"], title=item["title"],
                    state=item["state"]))  # fmt: skip
            for sug in item["suggestions"]:
                age = "X" if sug["x"] else f"{sug['age']}+"
                print(t("rate.suggestion_line", edition=sug["edition_id"], age=age,
                        provenance=sug["provenance"]))  # fmt: skip
        if args.confirm:
            print(t("rate.pending_needs_confirm", token=listed["token"]), file=sys.stderr)
        else:
            print(t("rate.pending_confirm", age=listed["age"], token=listed["token"]))
        return EXIT_NEEDS_CONFIRMATION
    done = channel.call("ops.rate_batch", age=args.age, confirm=args.confirm)
    print(t("rate.batch_done", count=len(done["rated"]), age=done["age"]))
    _withdrawals([w for r in done["rated"] for w in r["withdrawals"]])
    _suggestions([x for r in done["rated"] for x in r["open_suggestions"]])
    return 0
