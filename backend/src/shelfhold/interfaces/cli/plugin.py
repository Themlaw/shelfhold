"""``shelfhold plugin install|enable|disable|remove|list|attest|withdraw|settings|set|set-secret|
set-cookies`` (EF-SRC-019, ENF-SEC-009, EF-SRC-013, EF-EXP-065).

Third-party plugins are off by default. ``install`` only unpacks a local wheel whose sha256 you
give; ``enable`` needs ``SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1`` in the environment and a sentence
typed on standard input; every step is written to the audit log, and a refusal too.

``attest`` gives the admin's word for a trust value a third-party plugin declares
(``content_trust publisher``, ``access_partitioning library|user``, ``egress foyer``), for its
installed version only; ``withdraw`` takes it back. ``settings`` shows a source's settings, and of a
secret only whether it is set and its fingerprint; ``set-secret`` and ``set-cookies`` read the
value from a file or stdin, never from argv. All of them are audited.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from shelfhold.config import ResolvedSettings
from shelfhold.headless.actor import Actor
from shelfhold.i18n import t
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.errors import SecretsError
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.interfaces.cli._headless import read_secret
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.attestation import (
    AttestationError,
    AttestationNotNeeded,
    AttestationStore,
)
from shelfhold.plugin_host.public import installed_versions, start_plugins
from shelfhold.plugin_host.settings import (
    SettingsSchema,
    SettingValueError,
    SourceSettings,
    parse_settings_schema,
)


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

    attest = actions.add_parser("attest", help=t("cli.plugin_attest_help"), allow_abbrev=False)
    attest.add_argument("key", help=t("cli.plugin_key_help"))
    attest.add_argument("field", choices=("content_trust", "access_partitioning", "egress"))
    attest.add_argument("value", help=t("cli.plugin_attest_value_help"))
    attest.set_defaults(handler=run_attest)
    withdraw = actions.add_parser(
        "withdraw", help=t("cli.plugin_withdraw_help"), allow_abbrev=False
    )
    withdraw.add_argument("key", help=t("cli.plugin_key_help"))
    withdraw.add_argument("field", choices=("content_trust", "access_partitioning", "egress"))
    withdraw.set_defaults(handler=run_withdraw)

    show = actions.add_parser("settings", help=t("cli.plugin_settings_help"), allow_abbrev=False)
    show.add_argument("key", help=t("cli.plugin_key_help"))
    show.set_defaults(handler=run_settings)
    change = actions.add_parser("set", help=t("cli.plugin_set_help"), allow_abbrev=False)
    change.add_argument("key", help=t("cli.plugin_key_help"))
    change.add_argument("assignments", nargs="+", metavar="NAME=JSON")
    change.set_defaults(handler=run_set)
    secret = actions.add_parser(
        "set-secret", help=t("cli.plugin_set_secret_help"), allow_abbrev=False
    )
    secret.add_argument("key", help=t("cli.plugin_key_help"))
    secret.add_argument("field", help=t("cli.plugin_field_help"))
    secret.add_argument("--secret-file", required=True, help=t("cli.source_secret_file_help"))
    secret.set_defaults(handler=run_set_secret)
    jar = actions.add_parser(
        "set-cookies", help=t("cli.plugin_set_cookies_help"), allow_abbrev=False
    )
    jar.add_argument("key", help=t("cli.plugin_key_help"))
    jar.add_argument("--cookies-file", required=True, help=t("cli.plugin_cookies_file_help"))
    jar.set_defaults(handler=run_set_cookies)


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
    _sync_attestations(resolved)  # a new version: the word given for the old one lapses
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
    name = guard.normalise_name(args.name)
    keys = [k for k, v in installed_versions(resolved.values).items() if v.dist == name]
    try:
        guard.remove_plugin(layout, args.name)
    except guard.PluginInstallError as exc:
        return _fail(t("plugin.enable_refused", code=exc.code, detail=exc.detail))
    engine = _engine(resolved)
    if engine is not None:
        try:  # a reinstall later is new code: nothing attested survives the removal
            store = AttestationStore(engine)
            for key in keys:
                store.revoke(key)
        finally:
            engine.dispose()
    print(t("plugin.removed", name=args.name))
    return 0


def _sync_attestations(resolved: ResolvedSettings) -> None:
    engine = _engine(resolved)
    if engine is None:
        return
    try:
        AttestationStore(engine).sync_installed(installed_versions(resolved.values))
    finally:
        engine.dispose()


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


# -- attestations and settings (EF-EXP-065, EF-SRC-013) --------------------------------------


def _actor() -> Actor:
    return Actor(f"cli:uid{os.getuid()}")


def _engine(resolved: ResolvedSettings) -> Any:
    path = resolved.values.database_path
    if not path.exists():
        print(t("plugin.no_database"), file=sys.stderr)
        return None
    return create_db_engine(path)


def run_attest(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    engine = _engine(resolved)
    if engine is None:
        return 1
    try:
        store = AttestationStore(engine)
        store.sync_installed(installed_versions(resolved.values))  # the version of today
        given = store.attest(args.key, args.field, args.value, actor=_actor())
    except AttestationNotNeeded:
        return _fail(t("errors.plugin.attestation_not_needed.title"))
    except AttestationError as exc:
        return _fail(t("plugin.attest_refused", detail=exc))
    finally:
        engine.dispose()
    print(t("plugin.attested", plugin=given.plugin_key, field=given.field, value=given.value,
            version=given.plugin_version))  # fmt: skip
    return 0


def run_withdraw(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    engine = _engine(resolved)
    if engine is None:
        return 1
    try:
        done = AttestationStore(engine).withdraw(args.key, args.field, actor=_actor())
    finally:
        engine.dispose()
    if done:
        print(t("plugin.withdrawn", plugin=args.key))
    else:
        print(t("plugin.nothing_attested", plugin=args.key))
    return 0


def _schema(resolved: ResolvedSettings, key: str) -> SettingsSchema | None:
    """The schema of a loaded plugin: the same start-up load as the server."""
    registry, _records = start_plugins(resolved.values)
    record = registry.get(key)
    if record is None or record.manifest is None or record.status.value == "incompatible":
        print(t("errors.plugin.unknown.title"), file=sys.stderr)
        return None
    return parse_settings_schema(record.manifest.settings_schema)


def _settings_store(resolved: ResolvedSettings) -> SourceSettings | None:
    engine = _engine(resolved)
    if engine is None:
        return None
    try:
        vault = SecretVault(boot_check(resolved.values, create=False, announce=False))
    except SecretsError as exc:
        engine.dispose()
        print(t("plugin.keyring_unreadable", detail=exc), file=sys.stderr)
        return None
    return SourceSettings(engine, vault)


def _show(store: SourceSettings, key: str, schema: SettingsSchema) -> None:
    view = store.view(key, schema)
    for name, value in sorted(view.values.items()):
        print(t("plugin.setting_row", name=name, value=json.dumps(value, ensure_ascii=False)))
    for name, state in sorted([*view.secrets.items(), ("cookies", view.cookies)]):
        shown = (
            t("plugin.secret_set", fingerprint=state.fingerprint)
            if state.set
            else t("plugin.secret_unset")
        )
        print(t("plugin.setting_row", name=name, value=shown))


def _with_store(
    resolved: ResolvedSettings, key: str, act: Any, *, needs_schema: bool = True
) -> int:
    schema = _schema(resolved, key)
    if schema is None:
        return 1
    store = _settings_store(resolved)
    if store is None:
        return 1
    try:
        act(store, schema)
        _show(store, key, schema)
    except SettingValueError as exc:
        return _fail(t("plugin.setting_refused", field=exc.field, reason=exc.reason))
    finally:
        store.engine.dispose()
    return 0


def run_settings(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    return _with_store(resolved, args.key, lambda _store, _schema: None)


def run_set(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    values: dict[str, Any] = {}
    for assignment in args.assignments:
        name, sep, raw = assignment.partition("=")
        if not sep:
            return _fail(t("plugin.assignment_invalid", name=name))
        try:
            values[name] = json.loads(raw)
        except ValueError:
            values[name] = raw  # a bare word is a string
    return _with_store(
        resolved, args.key,
        lambda store, schema: store.set_values(args.key, schema, values, actor=_actor()),
    )  # fmt: skip


def run_set_secret(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    try:
        value = read_secret(args.secret_file)
    except OSError as exc:
        return _fail(t("headless.secret_unreadable", detail=exc))
    return _with_store(
        resolved, args.key,
        lambda store, schema: store.set_secret(args.key, schema, args.field, value,
                                               actor=_actor()),
    )  # fmt: skip


def run_set_cookies(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    try:
        raw = (
            sys.stdin.read()
            if args.cookies_file == "-"
            else Path(args.cookies_file).read_text(encoding="utf-8")
        )
        cookies = json.loads(raw)
    except (OSError, ValueError):
        return _fail(t("plugin.cookies_unreadable"))
    if not isinstance(cookies, dict):
        return _fail(t("plugin.cookies_unreadable"))
    return _with_store(
        resolved, args.key,
        lambda store, _schema: store.set_cookies(args.key, cookies, actor=_actor()),
    )  # fmt: skip
