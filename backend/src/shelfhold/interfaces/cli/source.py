"""``shelfhold source add suwayomi`` (EF-INT-024, EF-SRC-031).

The authentication mode is read from the server; a mode this release cannot use is refused.
The secret (``user:password``) is read from a file or stdin, never from argv.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._headless import fail, headless_command, read_secret
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import HeadlessCommands, OpsChannel, OpsRefusal


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("source", help=t("cli.source_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="source_command", required=True)
    add = actions.add_parser("add", help=t("cli.source_add_help"), allow_abbrev=False)
    kinds = add.add_subparsers(dest="source_kind", required=True)
    suwayomi = kinds.add_parser("suwayomi", help=t("cli.source_add_suwayomi_help"),
                                allow_abbrev=False)  # fmt: skip
    suwayomi.add_argument("--url", required=True, help=t("cli.source_url_help"))
    suwayomi.add_argument("--secret-file", help=t("cli.source_secret_file_help"))
    suwayomi.add_argument("--fix-suwayomi-settings", action="store_true",
                          help=t("cli.source_fix_suwayomi_settings_help"))  # fmt: skip
    suwayomi.set_defaults(handler=run_add_suwayomi, configure_logging=False)
    confirm = actions.add_parser("confirm-roles", help=t("cli.source_confirm_roles_help"),
                                 allow_abbrev=False)  # fmt: skip
    confirm.set_defaults(handler=run_confirm_roles, configure_logging=False)
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
    # EF-SRC-015, EF-SRC-004: one-shot acquisition
    search = actions.add_parser("search", help=t("ops.help_source_search"), allow_abbrev=False)
    search.add_argument("source_key")
    search.add_argument("query", nargs="?", default="", help=t("ops.help_search_query"))
    search.add_argument("--filter", action="append", default=[], metavar="CLE=VALEUR",
                        help=t("ops.help_search_filter"))  # fmt: skip
    search.add_argument("--cursor", help=t("ops.help_search_cursor"))
    search.set_defaults(handler=run_search, configure_logging=False)
    filters = actions.add_parser("filters", help=t("ops.help_source_filters"), allow_abbrev=False)
    filters.add_argument("source_key")
    filters.set_defaults(handler=run_filters, configure_logging=False)
    acquire = actions.add_parser("acquire", help=t("ops.help_source_acquire"), allow_abbrev=False)
    acquire.add_argument("source_key")
    acquire.add_argument("remote_id")
    acquire.add_argument("--unit", help=t("ops.help_acquire_unit"))
    acquire.set_defaults(handler=run_acquire, configure_logging=False)
    # EF-SRC-022: the bulk migration of the identity of a source's series (a reserved action)
    migrate = actions.add_parser("migrate-ids", help=t("ops.help_source_migrate_ids"),
                                 allow_abbrev=False)  # fmt: skip
    migrate.add_argument("source_key")
    migrate.add_argument("--from-scheme", required=True, help=t("ops.help_migrate_from_scheme"))
    migrate.add_argument("--to-scheme", required=True, help=t("ops.help_migrate_to_scheme"))
    mode = migrate.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help=t("ops.help_migrate_preview"))
    mode.add_argument("--apply", action="store_true", help=t("ops.help_migrate_apply"))
    migrate.add_argument("--confirm", metavar="FINGERPRINT", default=None,
                         help=t("ops.help_migrate_confirm"))  # fmt: skip
    migrate.set_defaults(handler=run_migrate_ids, configure_logging=False)
    by_url = commands.add_parser("acquire", help=t("ops.help_acquire_url"), allow_abbrev=False)
    by_url.add_argument("url")
    by_url.add_argument("--unit", help=t("ops.help_acquire_unit"))
    by_url.set_defaults(handler=run_acquire_url, configure_logging=False)


@headless_command
def run_add_suwayomi(args: argparse.Namespace, commands: HeadlessCommands) -> int:
    secret: str | None = None
    if args.secret_file:
        try:
            secret = read_secret(args.secret_file)
        except (OSError, UnicodeDecodeError) as error:
            return fail(t("headless.secret_unreadable", detail=error))
    added = commands.source_add_suwayomi(
        url=args.url, secret=secret, fix_suwayomi_settings=args.fix_suwayomi_settings
    )
    if added.created:
        print(t("headless.source_added", mode=added.auth_mode))
    else:
        print(t("headless.source_updated", mode=added.auth_mode))
    return 0


@headless_command
def run_confirm_roles(_args: argparse.Namespace, commands: HeadlessCommands) -> int:
    if commands.source_confirm_roles():
        print(t("headless.roles_confirmed"))
    else:
        print(t("headless.roles_nothing_to_confirm"))
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


def _filters_of(pairs: list[str]) -> dict[str, str]:
    return {k: v for k, _, v in (p.partition("=") for p in pairs) if k}


@ops_command
def run_search(args: argparse.Namespace, channel: OpsChannel) -> int:
    params: dict[str, object] = {"source_key": args.source_key}
    if args.query:
        params["query"] = args.query
    if args.filter:
        params["filters"] = _filters_of(args.filter)
    if args.cursor:
        params["cursor"] = args.cursor
    answer = channel.call("ops.source_search", **params)
    for item in answer["items"]:
        print(t("ops.search_item", **item))
    if not answer["items"]:
        print(t("ops.search_nothing"))
    if answer["next"]:
        print(t("ops.search_next", cursor=answer["next"]))
    return 0


@ops_command
def run_filters(args: argparse.Namespace, channel: OpsChannel) -> int:
    answer = channel.call("ops.source_filters", source_key=args.source_key)
    for spec in answer["filters"]:
        print(t("ops.filters_item", name=spec["key"], kind=spec["kind"], label=spec["label"]))
    if not answer["filters"]:
        print(t("ops.filters_none"))
    return 0


def _acquired(answer: dict[str, object]) -> int:
    if answer["job_id"] is None:
        print(t("ops.acquire_stored"))
    else:
        print(t("ops.acquire_queued", job=answer["job_id"]))
    return 0


@ops_command
def run_acquire(args: argparse.Namespace, channel: OpsChannel) -> int:
    params: dict[str, object] = {"source_key": args.source_key, "remote_id": args.remote_id}
    if args.unit:
        params["unit"] = args.unit
    return _acquired(channel.call("ops.acquire", **params))


@ops_command
def run_acquire_url(args: argparse.Namespace, channel: OpsChannel) -> int:
    params: dict[str, object] = {"url": args.url}
    if args.unit:
        params["unit"] = args.unit
    return _acquired(channel.call("ops.acquire", **params))


STALE_PREVIEW = 4


@ops_command
def run_migrate_ids(args: argparse.Namespace, channel: OpsChannel) -> int:
    """`--preview` reads and writes nothing; `--apply` needs the fingerprint of a preview."""
    if args.apply and not args.confirm:
        print(t("ops.migrate_ids_needs_confirm"), file=sys.stderr)
        return 2
    if args.preview and args.confirm:
        print(t("ops.migrate_ids_preview_has_no_confirm"), file=sys.stderr)
        return 2
    params: dict[str, object] = {
        "source_key": args.source_key, "from_scheme": args.from_scheme,
        "to_scheme": args.to_scheme,
    }  # fmt: skip
    if args.apply:
        params["confirm"] = args.confirm
    try:
        done = channel.call("ops.source_migrate_ids", **params)
    except OpsRefusal as refusal:
        if refusal.code != "ops.preview_stale":
            raise
        print(t("ops.migrate_ids_preview_stale"), file=sys.stderr)
        return STALE_PREVIEW
    held = done["migrate"] + done["blocked"] + done["unresolved"]
    forms = done["new_forms"]
    if not held and not forms:
        print(t("ops.migrate_ids_nothing"))
        return 0
    print(t("ops.migrate_ids_header", migrate=len(done["migrate"]), blocked=len(done["blocked"]),
            unresolved=len(done["unresolved"])))  # fmt: skip
    if forms:
        if done["applied"] is None:
            print(t("ops.migrate_ids_forms_to_seal", forms=forms))
        else:
            print(t("ops.migrate_ids_forms_sealed", forms=forms))
    for outcome in ("migrate", "blocked", "unresolved"):
        for row in done[outcome]:
            print(t("ops.migrate_ids_row", outcome=outcome, id=row["id"], old=row["old"],
                    new=row["new"] or "-", reason=row["reason"]))  # fmt: skip
    if done["blocked"]:
        print(t("ops.migrate_ids_blocked_hint"))
    if done["applied"] is None:
        print(t("ops.migrate_ids_fingerprint", fingerprint=done["fingerprint"]))
    else:
        print(t("ops.migrate_ids_applied", applied=done["applied"]))
    return 0
