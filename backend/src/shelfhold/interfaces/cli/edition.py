"""``shelfhold edition set-lang``: give a language to an Edition that has none (ENF-I18N-005)."""

from __future__ import annotations

import argparse

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
