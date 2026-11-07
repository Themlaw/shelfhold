"""``shelfhold plugin install|enable|disable|remove|list`` (EF-SRC-019, ENF-SEC-009).

Third-party plugins are off by default. ``install`` only unpacks a local wheel whose sha256 you
give; ``enable`` needs ``SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1`` in the environment and a sentence
typed on standard input; every step is written to the audit log, and a refusal too.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from shelfhold.config import ResolvedSettings
from shelfhold.i18n import t
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.public import start_plugins


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("plugin", help=t("cli.plugin_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="plugin_command", required=True)

    add = actions.add_parser("install", help=t("cli.plugin_install_help"), allow_abbrev=False)
    add.add_argument("wheel", type=Path, help=t("cli.plugin_wheel_help"))
    add.add_argument("--sha256", required=True, help=t("cli.plugin_sha256_help"))
    add.set_defaults(handler=run_install)

    for name, handler, help_text in (
        ("enable", run_enable, t("cli.plugin_enable_help")),
        ("disable", run_disable, t("cli.plugin_disable_help")),
        ("remove", run_remove, t("cli.plugin_remove_help")),
    ):
        sub = actions.add_parser(name, help=help_text, allow_abbrev=False)
        sub.add_argument("name", help=t("cli.plugin_name_help"))
        sub.set_defaults(handler=handler)

    listing = actions.add_parser("list", help=t("cli.plugin_list_help"), allow_abbrev=False)
    listing.add_argument("--load", action="store_true", help=t("cli.plugin_load_help"))
    listing.set_defaults(handler=run_list)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _audited(resolved: ResolvedSettings, action: str, target: str) -> bool:
    """False when the audit row cannot be written: nothing is then changed (fail closed)."""
    path = resolved.values.database_path
    if not path.exists():
        print(t("plugin.no_database"), file=sys.stderr)
        return False
    engine = create_db_engine(path)
    try:
        guard.audit(engine, action, target)
    except Exception as exc:  # the database is the audit trail: no trail, no change
        print(t("plugin.audit_failed", detail=exc), file=sys.stderr)
        return False
    finally:
        engine.dispose()
    return True


def run_install(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    layout = guard.Layout.of(resolved.values.data_dir)
    try:
        plugin = guard.install_wheel(layout, args.wheel, args.sha256)
    except guard.PluginInstallError as exc:
        _audited(resolved, "plugin.install_refused", f"{args.wheel.name}:{exc.code}")
        return _fail(t("plugin.install_refused", code=exc.code, detail=exc.detail))
    if not _audited(
        resolved, "plugin.install", f"{plugin.name}@{plugin.version}:{plugin.wheel_sha256}"
    ):
        guard.remove_plugin(layout, plugin.name)  # no audit row, no installation
        return 1
    print(t("plugin.installed", name=plugin.name, version=plugin.version))
    print(t("plugin.disabled_by_default"))
    return 0


def run_enable(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    layout = guard.Layout.of(resolved.values.data_dir)
    if not guard.env_allows(os.environ):
        _audited(resolved, "plugin.enable_refused", f"{args.name}:env")
        return _fail(t("plugin.env_required", variable=guard.ENV_ALLOW))
    print(t("plugin.warning", phrase=guard.CONFIRMATION), file=sys.stderr)
    typed = sys.stdin.readline()
    state = guard.read_state(layout)
    plugin = state.installed.get(guard.normalise_name(args.name))
    target = f"{args.name}@{plugin.wheel_sha256}" if plugin else args.name
    if plugin is not None and typed.strip() != guard.CONFIRMATION:
        _audited(resolved, "plugin.enable_refused", f"{target}:confirmation")
        return _fail(t("plugin.confirmation_mismatch"))
    if not _audited(resolved, "plugin.enable", target):  # before the change, not after
        return 1
    try:
        guard.confirm(layout, args.name, typed)
    except guard.PluginInstallError as exc:
        return _fail(t("plugin.enable_refused", code=exc.code, detail=exc.detail))
    print(t("plugin.enabled", name=args.name))
    return 0


def run_disable(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    if not _audited(resolved, "plugin.disable", args.name):
        return 1
    guard.unconfirm(guard.Layout.of(resolved.values.data_dir), args.name)
    print(t("plugin.disabled", name=args.name))
    return 0


def run_remove(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    layout = guard.Layout.of(resolved.values.data_dir)
    if not _audited(resolved, "plugin.remove", args.name):
        return 1
    try:
        guard.remove_plugin(layout, args.name)
    except guard.PluginInstallError as exc:
        return _fail(t("plugin.enable_refused", code=exc.code, detail=exc.detail))
    print(t("plugin.removed", name=args.name))
    return 0


def run_list(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    layout = guard.Layout.of(resolved.values.data_dir)
    state = guard.read_state(layout)
    if not state.installed:
        print(t("plugin.none"))
        return 0
    if args.load:  # the same start-up load as the server, then the verdict of each plugin
        _registry, records = start_plugins(resolved.values)
        for record in records:
            print(
                t(
                    "plugin.row_loaded",
                    plugin=record.key,
                    status=record.status.value,
                    reason=record.reason or "",
                )
            )
        return 0
    for verdict in guard.verdicts(layout, os.environ):
        plugin = state.installed[verdict.name]
        status = (
            t("plugin.status_ready")
            if verdict.granted
            else t("plugin.status_disabled", reason=verdict.reason)
        )
        print(t("plugin.row", name=plugin.name, version=plugin.version, status=status))
    return 0
