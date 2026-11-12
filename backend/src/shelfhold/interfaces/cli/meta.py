"""``shelfhold meta rebuild|check``: the projections of the database (ENF-OPS-020, EF-EXP-063).

The database is the authority; `unit.json` and `work.json` in `/data/meta` and the provenance
mirror of each root are regenerated from it, and a file of a newer format than this build is
reported and never rewritten (ENF-OPS-013). A mirror line the database cannot explain is kept,
flagged `unresolved`. The command goes through the instance (its socket, else its lock), because
the mirror lives on the library roots and is written under the write lease.
"""

from __future__ import annotations

import argparse
import sys

from shelfhold.i18n import t
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("meta", help=t("cli.meta_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="meta_command", required=True)
    rebuild = actions.add_parser("rebuild", help=t("cli.meta_rebuild_help"), allow_abbrev=False)
    rebuild.add_argument("--unit", help=t("cli.meta_unit_help"))
    rebuild.set_defaults(handler=run_rebuild)
    enrich = actions.add_parser("enrich", help=t("cli.meta_enrich_help"), allow_abbrev=False)
    enrich.add_argument("target", help=t("cli.meta_target_help"))
    enrich.set_defaults(handler=run_enrich)
    for name, help_key in (("lock", "cli.meta_lock_help"), ("unlock", "cli.meta_unlock_help")):
        lock = actions.add_parser(name, help=t(help_key), allow_abbrev=False)
        lock.add_argument("edition", help=t("cli.meta_edition_help"))
        lock.add_argument("field", help=t("cli.meta_field_help"))
        if name == "lock":
            lock.add_argument("value", nargs="?", default=None, help=t("cli.meta_value_help"))
        lock.add_argument("--lang", default="und", help=t("cli.meta_lang_help"))
        lock.add_argument("--on", default="edition", help=t("cli.meta_on_help"))
        lock.set_defaults(handler=run_lock if name == "lock" else run_unlock)
    check = actions.add_parser("check", help=t("cli.meta_check_help"), allow_abbrev=False)
    check.set_defaults(handler=run_check)


def _newer(done: dict[str, list[str]]) -> None:
    for unit_id in done["units_read_only"]:
        print(t("meta.newer_format", unit=unit_id))
    for work_id in done["works_read_only"]:
        print(t("meta.work_newer_format", work=work_id))
    for name in done["mirror_skipped"]:
        print(t("meta.mirror_skipped", name=name))


@ops_command
def run_rebuild(args: argparse.Namespace, channel: OpsChannel) -> int:
    if args.unit is not None:
        channel.call("ops.meta_rebuild", unit=args.unit)
        return 0
    done = channel.call("ops.meta_rebuild", unit=None)
    print(
        t("meta.rebuilt", written=len(done["units"]), works=len(done["works"]),
          mirror=len(done["mirror"]))
    )  # fmt: skip
    _newer(done)
    return 0


@ops_command
def run_check(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.meta_check")
    for unit_id in done["units"]:
        print(t("meta.drift", unit=unit_id))
    for work_id in done["works"]:
        print(t("meta.work_drift", work=work_id))
    for name in done["mirror"]:
        print(t("meta.mirror_drift", name=name))
    _newer(done)
    drift = done["units"] or done["works"] or done["mirror"]
    if drift:
        print(t("meta.run_rebuild"), file=sys.stderr)
    return 1 if drift else 0


@ops_command
def run_lock(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.meta_lock", edition=args.edition, field=args.field,
                        value=args.value, lang=args.lang, on=args.on)  # fmt: skip
    key = "meta.locked_kept" if done["kept"] else "meta.locked"
    print(t(key, edition=done["edition_id"], field=done["field"], lang=done["lang"], on=done["on"]))
    return 0


@ops_command
def run_unlock(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.meta_unlock", edition=args.edition, field=args.field,
                        lang=args.lang, on=args.on)  # fmt: skip
    print(t("meta.unlocked", edition=done["edition_id"], field=done["field"], lang=done["lang"],
            on=done["on"]))  # fmt: skip
    return 0


@ops_command
def run_enrich(args: argparse.Namespace, channel: OpsChannel) -> int:
    done = channel.call("ops.meta_enrich", target=args.target)
    outcome = done["outcome"]
    if outcome != "enriched":
        reason = done["reason"] or ""
        if outcome == "provider_down":
            message = t("meta.enrich_provider_down", reason=reason)
        elif outcome == "robots_disallowed":
            message = t("meta.enrich_robots_disallowed")
        elif outcome == "quota":
            message = t("meta.enrich_quota")
        else:
            message = t("meta.enrich_not_found")
        print(message, file=sys.stderr)
        return 1
    print(t("meta.enriched", edition=done["edition_id"], count=len(done["written"])))
    for name in done["written"]:
        print(t("meta.enrich_field", field=name))
    for name in done["kept_locked"] + done["kept_manual"]:
        print(t("meta.enrich_kept", field=name))
    for age in done["ages"]:
        label = t("meta.enrich_age_x") if age["x"] else f"{age['age']}+"
        print(t("meta.enrich_age_suggested", age=label))
    if done["cover_rejected"]:
        print(t("meta.enrich_cover_rejected", code=done["cover_rejected"]))
    print(
        t("meta.enrich_attribution", text=done["attribution"] or "", url=done["source_url"] or "")
    )
    return 0
