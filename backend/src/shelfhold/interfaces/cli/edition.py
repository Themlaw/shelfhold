"""``shelfhold edition``: a language for an Edition that has none (ENF-I18N-005), its exported
series name, and the attaches by external identifier (L12): ``attachments`` lists them, ``detach``
undoes one ("Séparer"), ``attach-confirm`` prints the removal plan of a suggested attach and
applies it only for the token of that plan, ``attach-decline`` refuses it."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("edition", help=t("ops.help_edition"), allow_abbrev=False)
    actions = group.add_subparsers(dest="edition_command", required=True)
    setter = actions.add_parser("set-lang", help=t("ops.help_edition_set_lang"), allow_abbrev=False)
    setter.add_argument("edition_id")
    setter.add_argument("lang", help=t("ops.help_lang"))
    setter.set_defaults(handler=run_set_lang, configure_logging=False)
    namer = actions.add_parser(
        "set-series-name", help=t("ops.help_edition_set_series_name"), allow_abbrev=False
    )
    namer.add_argument("edition_id")
    namer.add_argument("name", help=t("ops.help_series_name"))
    namer.set_defaults(handler=run_set_series_name, configure_logging=False)
    listing = actions.add_parser(
        "attachments", help=t("ops.help_edition_attachments"), allow_abbrev=False
    )
    listing.set_defaults(handler=run_attachments, configure_logging=False)
    detach = actions.add_parser("detach", help=t("ops.help_edition_detach"), allow_abbrev=False)
    detach.add_argument("attach_id")
    detach.set_defaults(handler=run_detach, configure_logging=False)
    confirm = actions.add_parser(
        "attach-confirm", help=t("ops.help_edition_attach_confirm"), allow_abbrev=False
    )
    confirm.add_argument("attach_id")
    confirm.add_argument("--confirm", default=None, help=t("ops.help_edition_attach_token"))
    confirm.set_defaults(handler=run_attach_confirm, configure_logging=False)
    decline = actions.add_parser(
        "attach-decline", help=t("ops.help_edition_attach_decline"), allow_abbrev=False
    )
    decline.add_argument("attach_id")
    decline.set_defaults(handler=run_attach_decline, configure_logging=False)


@ops_command
def run_set_lang(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.edition_set_lang", edition_id=args.edition_id, lang=args.lang)
    print(t("ops.language_set", lang=answer["lang"]))
    return 0


@ops_command
def run_set_series_name(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.edition_set_series_name", edition_id=args.edition_id, name=args.name)
    print(t("ops.series_name_set", name=answer["name"], series=answer["export_series_id"]))
    return 0


@ops_command
def run_attachments(_args: argparse.Namespace, channel: OpsChannel) -> int:
    listed = channel.call("ops.attach_list")
    if not listed:
        print(t("ops.attach_none"))
    for a in listed:
        print(t("ops.attach_line", attach=a["attach_id"], edition=a["edition_id"],
                work=a["work_id"], origin=a["from_work_id"], state=a["state"]))  # fmt: skip
    return 0


@ops_command
def run_detach(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.attach_detach", attach_id=args.attach_id)
    print(t("ops.attach_detached", edition=done["edition_id"], work=done["work_id"]))
    if done["to_rate"]:
        print(t("ops.attach_to_rate"))
    for field in done["kept"]:
        print(t("ops.attach_kept", field=field))
    for r in done["refile"]:
        print(t("ops.attach_refile", edition=r["edition_id"], shelf=r["shelf_id"]))
    for provider, value in done["pending_ids"]:
        print(t("ops.attach_pending_id", provider=provider, value=value))
    return 0


def _print_plan(plan: dict[str, Any]) -> None:
    withdrawals: list[dict[str, str]] = plan["withdrawals"]
    print(t("rate.withdrawals_plan", count=len(withdrawals)))
    for w in withdrawals:
        print(t("rate.withdrawal_line", edition=w["edition_id"], shelf=w["shelf_id"],
                reason=w["reason"]))  # fmt: skip


@ops_command
def run_attach_confirm(args: argparse.Namespace, channel: OpsChannel) -> int:
    if not args.confirm:
        plan = channel.call("ops.attach_plan", attach_id=args.attach_id)
        _print_plan(plan)
        print(t("ops.attach_confirm_with", attach=args.attach_id, token=plan["token"]))
        return 2
    done = channel.call("ops.attach_confirm", attach_id=args.attach_id, confirm=args.confirm)
    print(t("ops.attach_confirmed", edition=done["edition_id"], work=done["work_id"]))
    if done["withdrawals"]:
        _print_plan(done)
        print(t("ops.attach_withdraw_next"), file=sys.stderr)
    return 0


@ops_command
def run_attach_decline(args: argparse.Namespace, channel: OpsChannel) -> int:
    channel.call("ops.attach_decline", attach_id=args.attach_id)
    print(t("ops.attach_declined", attach=args.attach_id))
    return 0
