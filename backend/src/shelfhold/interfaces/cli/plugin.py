"""``shelfhold plugin install|enable|disable|remove|adopt|list|attest|withdraw|settings|set|
set-secret|set-cookies`` and ``plugin index add|list|refresh|remove`` (EF-SRC-019, ENF-SEC-009,
EF-SRC-013, EF-EXP-065, EF-SRC-017).

Third-party plugins are off by default and isolated is their mode (D13, M6 L06). ``install``
unpacks a local wheel whose sha256 you give, or — with ``--index`` — installs the plugin a pinned
signed index vouches for, its key pinned to (index, signer): another signer publishing the same
key is a distinct install, never an update, and the state, the settings and the secrets of the
previous install move only on the admin's confirmed word (``adopt``, a reserved action);
``enable`` is a **reserved action** — the host CLI is
one of its two credentials — and activates the plugin in its mode: isolated needs nothing else,
the in-process mode (``--in-process``) needs ``SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1`` in the
environment and a sentence typed on standard input, and no phrase ever crosses the network. Every
step is written to the audit log, and a refusal too; ``list`` says why a plugin is not loaded.

``attest`` gives the admin's word for a trust value a third-party plugin declares
(``content_trust publisher``, ``access_partitioning library|user``, ``egress foyer``), for its
installed version only; ``withdraw`` takes it back. ``settings`` shows a source's settings, and of a
secret only whether it is set and its fingerprint; ``set-secret`` and ``set-cookies`` read the
value from a file or stdin, never from argv. All of them are audited.

``index`` pins a repository of signed plugin indexes (D14): by its URL **and** the public key of
its signer — never without the key, and none is preconfigured. A refresh goes through the host's
Fetcher (SSRF guard) and verifies the minisign signature, the trusted comment, the monotone
version, the expiry and the revocations; a rotation moves the pin only on a double signature.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shelfhold.config import ResolvedSettings
from shelfhold.headless.actor import Actor
from shelfhold.i18n import t
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.errors import SecretsError
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.integrations.trackers import purge_plugin
from shelfhold.interfaces.cli._headless import read_secret
from shelfhold.interfaces.cli._ops import ops_command
from shelfhold.interfaces.local_cli import OpsChannel
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host import lifecycle
from shelfhold.plugin_host.attestation import (
    AttestationError,
    AttestationNotNeeded,
    AttestationStore,
)
from shelfhold.plugin_host.index import (
    PluginIndexError,
    RepoStore,
    hold_previous_install,
    index_fetcher,
)
from shelfhold.plugin_host.public import (
    installed_keys,
    installed_versions,
    refuse_taken_keys,
    start_plugins,
)
from shelfhold.plugin_host.settings import (
    LNCRAWL_RIGHTS_WARNING,
    USE_READBACK,
    PluginDataError,
    SettingsSchema,
    SettingValueError,
    SourceSettings,
    adopt_plugin_data,
    held_plugin_data,
    parse_settings_schema,
    write_audit,
)


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = commands.add_parser("plugin", help=t("cli.plugin_help"), allow_abbrev=False)
    actions = group.add_subparsers(dest="plugin_command", required=True)

    add = actions.add_parser("install", help=t("cli.plugin_install_help"), allow_abbrev=False)
    add.add_argument("wheel", help=t("cli.plugin_wheel_help"))
    add.add_argument("--sha256", help=t("cli.plugin_sha256_help"))
    add.add_argument("--index", help=t("cli.plugin_install_index_help"))
    add.set_defaults(handler=run_install)

    adopt = actions.add_parser("adopt", help=t("cli.plugin_adopt_help"), allow_abbrev=False)
    adopt.add_argument("key", help=t("cli.plugin_key_help"))
    adopt.add_argument("--held", help=t("cli.plugin_adopt_held_help"))
    adopt.set_defaults(handler=run_adopt)

    for name, handler, help_text in (
        ("enable", run_enable, t("cli.plugin_enable_help")),
        ("disable", run_disable, t("cli.plugin_disable_help")),
        ("remove", run_remove, t("cli.plugin_remove_help")),
    ):
        sub = actions.add_parser(name, help=help_text, allow_abbrev=False)
        sub.add_argument("name", help=t("cli.plugin_name_help"))
        if name == "enable":  # D13 (M6 L06): the admin chooses the mode, never the plugin
            sub.add_argument(
                "--in-process",
                action="store_true",
                help=t("cli.plugin_enable_in_process_help"),
            )
            sub.add_argument(
                "--isolated", action="store_true", help=t("cli.plugin_enable_isolated_help")
            )
        sub.set_defaults(handler=handler)

    # M6 L09a (EF-SRC-020, D15): the lifecycle of an installed plugin — by its key, like the
    # index and the attestations, not by its distribution name
    for name, handler, help_text in (
        ("update", run_update, t("cli.plugin_update_help")),
        ("pin", run_pin, t("cli.plugin_pin_help")),
        ("unpin", run_unpin, t("cli.plugin_unpin_help")),
        ("rollback", run_rollback, t("cli.plugin_rollback_help")),
    ):
        sub = actions.add_parser(name, help=help_text, allow_abbrev=False)
        sub.add_argument("key", help=t("cli.plugin_key_help"))
        sub.set_defaults(handler=handler)

    # M6 L09b (EF-BIB-054 (4), EF-SRC-030): the sources of a plugin move to the replacement
    # its index named — a reserved action through the operating channel, like the others
    migrate = actions.add_parser(
        "migrate-to", help=t("cli.plugin_migrate_to_help"), allow_abbrev=False
    )
    migrate.add_argument("key", help=t("cli.plugin_key_help"))
    migrate.add_argument("replacement", help=t("cli.plugin_replacement_help"))
    migrate.set_defaults(handler=run_migrate_to)

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

    # M6 L07 (EF-SRC-017, D14): the repositories of signed plugin indexes, the admin's alone
    index = actions.add_parser("index", help=t("cli.plugin_index_help"), allow_abbrev=False)
    index_actions = index.add_subparsers(dest="plugin_index_command", required=True)

    index_add = index_actions.add_parser(
        "add", help=t("cli.plugin_index_add_help"), allow_abbrev=False
    )
    index_add.add_argument("url", help=t("cli.plugin_index_url_help"))
    index_add.add_argument("--pubkey", help=t("cli.plugin_index_pubkey_help"))
    index_add.set_defaults(handler=run_index_add)

    index_list = index_actions.add_parser(
        "list", help=t("cli.plugin_index_list_help"), allow_abbrev=False
    )
    index_list.set_defaults(handler=run_index_list)

    index_refresh = index_actions.add_parser(
        "refresh", help=t("cli.plugin_index_refresh_help"), allow_abbrev=False
    )
    index_refresh.add_argument("url", nargs="?", help=t("cli.plugin_index_url_help"))
    index_refresh.set_defaults(handler=run_index_refresh)

    index_remove = index_actions.add_parser(
        "remove", help=t("cli.plugin_index_remove_help"), allow_abbrev=False
    )
    index_remove.add_argument("url", help=t("cli.plugin_index_url_help"))
    index_remove.set_defaults(handler=run_index_remove)


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


def _vault_or_none(resolved: ResolvedSettings) -> SecretVault | None:
    """The keyring when it is there, else None: only a move of sealed secrets needs it, and the
    move refuses on its own when it does (fail closed)."""
    try:
        return SecretVault(boot_check(resolved.values, create=False, announce=False))
    except SecretsError:
        return None


def _store_of(engine: Any, resolved: ResolvedSettings) -> RepoStore:
    """The repositories' store, told where the plugins are installed: a refresh disables what an
    index revoked."""
    return RepoStore(
        engine, now=SystemClock().now, layout=guard.Layout.of(resolved.values.data_dir)
    )


def _held_audit(held: str, connection: Any) -> None:
    """The audit row of a hold, written in the hold's own transaction: the data is held the
    moment the row says so, whatever the install does next (M6 L08 review)."""
    write_audit(connection, "plugin.state_held", held, _actor())


def _lifecycle_audit(action: str, target: str) -> Callable[[Any], None]:
    """The audit line of a lifecycle act, handed to the act itself: it is written in the same
    transaction as the change it vouches for (M6 L09a)."""
    return lambda connection: write_audit(connection, action, target, _actor())


def _install_from_index(args: argparse.Namespace, resolved: ResolvedSettings, layout: Any) -> int:
    """Install a plugin a pinned index vouches for (M6 L08, EF-SRC-017 passe 8): the key is
    pinned to (index, signer), another signer is a distinct install, and the previous install's
    data waits for the admin's confirmed word."""
    if args.sha256:
        return _fail(t("plugin.sha256_with_index"))
    engine = _engine(resolved)
    if engine is None:
        return 1
    store = _store_of(engine, resolved)
    try:
        record = next((r for r in store.list() if r.url == args.index), None)
        if record is None:
            _audited(resolved, "plugin.install_refused", f"{args.wheel}:not_found")
            return _fail(t("plugin.index_refused", code="not_found", detail=""))
        from shelfhold.composition import default_network

        async def _run() -> Any:
            fetcher = index_fetcher(default_network())
            try:
                return await store.install_from(
                    record,
                    args.wheel,
                    layout,
                    fetcher,
                    vault=_vault_or_none(resolved),
                    held_audit=_held_audit,
                )
            finally:
                await fetcher.aclose()

        try:
            done = asyncio.run(_run())
        except (PluginIndexError, guard.PluginInstallError, PluginDataError) as exc:
            code = getattr(exc, "code", "install_failed")
            detail = getattr(exc, "detail", str(exc))
            _audited(resolved, "plugin.install_refused", f"{args.wheel}:{code}")
            return _fail(t("plugin.install_refused", code=code, detail=detail))
        target = f"{done.installed.name}@{done.installed.version}:{done.installed.wheel_sha256}"
        if not _audited(resolved, "plugin.install", target):
            guard.remove_plugin(layout, done.installed.name)  # no audit row, no installation
            return 1
        if done.held is not None:  # the previous install's data waits, it is not inherited
            print(t("plugin.state_held", name=args.wheel, held=done.held))
        if done.deprecated:  # the index's own word, read to the admin, never acted on
            if done.replaced_by:
                print(t("plugin.deprecated_replaced", name=args.wheel, replaced=done.replaced_by))
            else:
                print(t("plugin.deprecated", name=args.wheel))
    finally:
        engine.dispose()
    _sync_attestations(resolved)  # a new version: the word given for the old one lapses
    print(t("plugin.installed", name=done.installed.name, version=done.installed.version))
    print(t("plugin.disabled_by_default"))
    return 0


def run_install(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    """Install a wheel the admin gives by hand, or — with ``--index`` — one a signed index vouches
    for. A wheel by hand has no index and no signer: it is the admin's own word. What an earlier
    install of the same key left — pinned to a signer's index — is held aside under that signer's
    fingerprint, like an install from another signer (GAP-8-13): the code of this wheel was signed
    by nobody, and inherits neither the secrets nor the settings nor the state of code someone
    did sign. The bindings a removal left orphaned come back only when the previous install was
    the admin's own too."""
    layout = guard.Layout.of(resolved.values.data_dir)
    if args.index is not None:
        return _install_from_index(args, resolved, layout)
    if not args.sha256:
        return _fail(t("plugin.sha256_required"))
    wheel = Path(args.wheel)
    engine = _engine(resolved)
    if engine is None:
        return 1
    installed = False
    previous_of: dict[str, tuple[str | None, str | None] | None] = {}
    try:
        try:
            refuse_taken_keys(layout, wheel, args.sha256)  # one key, one install (S6)
            info = guard.inspect_wheel(wheel)
            keys = sorted(guard.wheel_plugin_keys(wheel))
            held_now: list[tuple[str, str]] = []
            # the previous install's data is held aside BEFORE the wheel is placed: the hand
            # wheel is never on disk while it could read the data of code someone signed. A
            # distribution already installed is left to the guard's refusal, its data stays put.
            if info.name not in guard.read_state(layout).installed:
                vault = _vault_or_none(resolved)
                for key in keys:
                    row = lifecycle.read_install(engine, key)
                    previous = None if row is None else (row.index_url, row.signer_pubkey)
                    held = hold_previous_install(
                        engine, vault, key, previous, (None, None), audit=_held_audit
                    )
                    if held is not None:
                        held_now.append((key, held))
                    previous_of[key] = previous
            plugin = guard.install_wheel(layout, wheel, args.sha256)
            installed = True
        except PluginDataError as exc:  # the data could not be held: no installation
            _audited(resolved, "plugin.install_refused", f"{wheel.name}:{exc.code}")
            return _fail(t("plugin.install_refused", code=exc.code, detail=exc.detail))
        except guard.PluginInstallError as exc:
            _audited(resolved, "plugin.install_refused", f"{wheel.name}:{exc.code}")
            return _fail(t("plugin.install_refused", code=exc.code, detail=exc.detail))
        try:
            if not _audited(
                resolved, "plugin.install", f"{plugin.name}@{plugin.version}:{plugin.wheel_sha256}"
            ):
                guard.remove_plugin(layout, plugin.name)  # no audit row, no installation
                installed = False
                return 1
            reattached = 0
            for key, held in held_now:
                print(t("plugin.state_held", name=key, held=held))
            store = _store_of(engine, resolved)
            for key in keys:
                store.unpin(key)  # a wheel by hand is pinned by the admin's own act (M6 L08)
                reattached += lifecycle.reattach_if_same_origin(
                    engine, key, previous_of.get(key), None, None
                )
        except BaseException:  # whatever fails after the placement: no wheel left behind
            if installed:
                guard.remove_plugin(layout, plugin.name)
            raise
    finally:
        engine.dispose()
    _sync_attestations(resolved)  # a new version: the word given for the old one lapses
    print(t("plugin.installed", name=plugin.name, version=plugin.version))
    print(t("plugin.disabled_by_default"))
    if reattached:  # the bindings a removal left orphaned follow the key, not the files
        print(t("plugin.reattached", count=reattached))
    return 0


def run_adopt(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    """The admin's confirmed transfer (a reserved action, EF-SRC-017 passe 8): the state, the
    settings and the secrets a previous install (another signer) left held move to the
    installed plugin. Nothing is overwritten: a plugin that already has data of its own is
    refused, and so is a key with nothing held. Several previous installs may wait under
    different signers: the transfer then names which one, never a silent pick."""
    layout = guard.Layout.of(resolved.values.data_dir)
    key = guard.normalise_name(args.key)
    if key not in installed_keys(layout):
        _audited(resolved, "plugin.adopt_refused", f"{key}:unknown")
        return _fail(t("errors.plugin.unknown.title"))
    engine = _engine(resolved)
    if engine is None:
        return 1
    try:
        found = held_plugin_data(engine, key)
        if not found:
            _audited(resolved, "plugin.adopt_refused", f"{key}:nothing_held")
            return _fail(t("plugin.nothing_held", name=key))
        if args.held is None and len(found) > 1:
            _audited(resolved, "plugin.adopt_refused", f"{key}:ambiguous")
            joined = t("status.list_separator").join(found)
            return _fail(t("plugin.adopt_ambiguous", name=key, held=joined))
        held = found[-1] if args.held is None else args.held
        if held not in found:
            _audited(resolved, "plugin.adopt_refused", f"{key}:unknown_held")
            return _fail(t("plugin.held_unknown", name=key, held=held))
        try:
            # the audit row and the move are one transaction: a refused transfer (occupied,
            # keyring) leaves no row that says the data moved (M6 L08 review)
            adopt_plugin_data(
                engine,
                _vault_or_none(resolved),
                key,
                held,
                audit=lambda connection: write_audit(connection, "plugin.adopt", held, _actor()),
            )
        except PluginDataError as exc:
            _audited(resolved, "plugin.adopt_refused", f"{key}:{exc.code}")
            return _fail(t("plugin.adopt_refused", code=exc.code, detail=exc.detail))
    finally:
        engine.dispose()
    print(t("plugin.adopted", name=key, held=held))
    return 0


def run_enable(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    """Enable a plugin in its mode (D13): isolated by default — a reserved action, no variable,
    no typed sentence; in process only with ``--in-process``, the variable of M1b and the phrase
    typed on standard input. A change of mode voids both activations, and a refusal — missing
    variable, wrong phrase — changes nothing: neither the mode nor the grants of the other mode."""
    layout = guard.Layout.of(resolved.values.data_dir)
    if args.in_process and args.isolated:
        return _fail(t("plugin.mode_exclusive"))
    state = guard.read_state(layout)
    plugin = state.installed.get(guard.normalise_name(args.name))
    mode = (
        guard.MODE_IN_PROCESS
        if args.in_process
        else guard.MODE_ISOLATED
        if args.isolated
        else plugin.mode
        if plugin is not None
        else guard.MODE_ISOLATED
    )
    typed = ""
    if mode == guard.MODE_IN_PROCESS:
        # the in-process ladder comes first, before anything is written: a refusal
        # must leave the recorded mode and the isolated grant exactly as they were
        if not guard.env_allows(os.environ):
            _audited(resolved, "plugin.enable_refused", f"{args.name}:env")
            return _fail(t("plugin.env_required", variable=guard.ENV_ALLOW))
        print(t("plugin.warning", phrase=guard.CONFIRMATION), file=sys.stderr)
        typed = sys.stdin.readline()
        if typed.strip() != guard.CONFIRMATION:
            _audited(resolved, "plugin.enable_refused", f"{args.name}:confirmation")
            return _fail(t("plugin.confirmation_mismatch"))
    target = f"{args.name}@{plugin.wheel_sha256}" if plugin else args.name
    if not _audited(resolved, "plugin.enable", target):  # before the change, not after
        return 1
    try:
        if mode == guard.MODE_IN_PROCESS:
            guard.set_mode(layout, args.name, guard.MODE_IN_PROCESS)
            guard.confirm(layout, args.name, typed)
            print(t("plugin.enabled", name=args.name))
        else:
            if args.isolated:  # back to the default: the in-process grant counts for nothing
                guard.set_mode(layout, args.name, guard.MODE_ISOLATED)
            guard.enable_isolated(layout, args.name)
            print(t("plugin.enabled_isolated", name=args.name))
    except guard.PluginInstallError as exc:
        return _fail(t("plugin.enable_refused", code=exc.code, detail=exc.detail))
    return 0


def run_disable(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    layout = guard.Layout.of(resolved.values.data_dir)
    state = guard.read_state(layout)
    plugin = state.installed.get(guard.normalise_name(args.name))
    loaded = (
        plugin is not None
        and plugin.mode == guard.MODE_IN_PROCESS
        and plugin.name in state.confirmed
    )
    if not _audited(resolved, "plugin.disable", args.name):
        return 1
    guard.unconfirm(layout, args.name)
    print(t("plugin.disabled", name=args.name))
    if loaded:
        # the host process loaded it at start-up: it goes on running until the next restart
        print(t("plugin.disable_restart_notice"))
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
    orphans = 0
    if engine is not None:
        try:  # a reinstall later is new code: nothing attested survives the removal
            store = AttestationStore(engine)
            for key in keys:
                store.revoke(key)
                # the bindings of the plugin's sources are marked orphaned, nothing they
                # hold is lost; a reinstall of the same key at the same origin reattaches
                orphans += lifecycle.retire_install(engine, layout, key)
            # nor a tracker link: the sealed tokens must not outlive the code they were
            # handed to, and the allowances said in the plugin's name go with them (M6 L13)
            purge_plugin(engine, keys)
        finally:
            engine.dispose()
    print(t("plugin.removed", name=args.name))
    if orphans:
        print(t("plugin.orphaned", count=orphans))
    return 0


def _installed_version(resolved: ResolvedSettings, key: str) -> Any | None:
    """The installed version of a plugin's key — the lifecycle speaks the key, like the index
    and the attestations, never the distribution name."""
    return installed_versions(resolved.values).get(key)


def run_update(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    """The admin's manual update (M6 L09a, D15): the version the pinned index vouches for now,
    fetched and verified like an install, the previous wheel kept aside for the rollback. A
    pinned plugin never moves, and a refusal — same version, lower one, revoked key or wheel —
    leaves the installed plugin exactly as it was."""
    layout = guard.Layout.of(resolved.values.data_dir)
    key = guard.normalise_name(args.key)
    version = _installed_version(resolved, key)
    if version is None:
        _audited(resolved, "plugin.update_refused", f"{key}:unknown")
        return _fail(t("errors.plugin.unknown.title"))
    engine = _engine(resolved)
    if engine is None:
        return 1
    try:
        store = _store_of(engine, resolved)
        from shelfhold.composition import default_network

        async def _run() -> Any:
            fetcher = index_fetcher(default_network())

            def _notice(entry: Any) -> None:  # the index's word, read to the admin
                if entry.replaced_by:
                    print(t("plugin.deprecated_replaced", name=key, replaced=entry.replaced_by))
                else:
                    print(t("plugin.deprecated", name=key))

            try:
                return await lifecycle.update(
                    engine,
                    store,
                    layout,
                    key,
                    version.dist,
                    fetcher,
                    audit=_lifecycle_audit("plugin.update", key),
                    notice=_notice,
                )
            finally:
                await fetcher.aclose()

        try:
            installed = asyncio.run(_run())
        except (
            lifecycle.PluginLifecycleError,
            PluginIndexError,
            guard.PluginInstallError,
        ) as exc:
            code = getattr(exc, "code", "update_failed")
            detail = getattr(exc, "detail", str(exc))
            _audited(resolved, "plugin.update_refused", f"{key}:{code}")
            return _fail(t("plugin.update_refused", code=code, detail=detail))
    finally:
        engine.dispose()
    _sync_attestations(resolved)  # a new version: the word given for the old one lapses
    print(t("plugin.updated", name=key, version=installed.version))
    return 0


def run_pin(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    """Freeze the installed version (M6 L09a): no update, no rollback, until the admin unpins."""
    layout = guard.Layout.of(resolved.values.data_dir)
    key = guard.normalise_name(args.key)
    version = _installed_version(resolved, key)
    if version is None:
        _audited(resolved, "plugin.pin_refused", f"{key}:unknown")
        return _fail(t("errors.plugin.unknown.title"))
    engine = _engine(resolved)
    if engine is None:
        return 1
    try:
        row = lifecycle.pin(
            engine, layout, key, version.dist, audit=_lifecycle_audit("plugin.pin", key)
        )
    except lifecycle.PluginLifecycleError as exc:
        _audited(resolved, "plugin.pin_refused", f"{key}:{exc.code}")
        return _fail(t("plugin.pin_refused", code=exc.code, detail=exc.detail))
    finally:
        engine.dispose()
    print(t("plugin.pinned", name=key, version=row.version))
    return 0


def run_unpin(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    """Take the pin back (M6 L09a): the updates and the rollbacks become possible again."""
    layout = guard.Layout.of(resolved.values.data_dir)
    key = guard.normalise_name(args.key)
    version = _installed_version(resolved, key)
    if version is None:
        _audited(resolved, "plugin.unpin_refused", f"{key}:unknown")
        return _fail(t("errors.plugin.unknown.title"))
    engine = _engine(resolved)
    if engine is None:
        return 1
    try:
        lifecycle.unpin(
            engine, layout, key, version.dist, audit=_lifecycle_audit("plugin.unpin", key)
        )
    except lifecycle.PluginLifecycleError as exc:
        _audited(resolved, "plugin.unpin_refused", f"{key}:{exc.code}")
        return _fail(t("plugin.unpin_refused", code=exc.code, detail=exc.detail))
    finally:
        engine.dispose()
    print(t("plugin.unpinned", name=key))
    return 0


def run_rollback(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    """Return to the previous kept version (M6 L09a, D15) — a reserved action, and the CLI is the
    host's own hand: the one version the last update put aside, and after it there is no
    previous anymore. A pinned plugin does not move, and a version the index revoked does not
    come back."""
    layout = guard.Layout.of(resolved.values.data_dir)
    key = guard.normalise_name(args.key)
    version = _installed_version(resolved, key)
    if version is None:
        _audited(resolved, "plugin.rollback_refused", f"{key}:unknown")
        return _fail(t("errors.plugin.unknown.title"))
    engine = _engine(resolved)
    if engine is None:
        return 1
    try:
        store = _store_of(engine, resolved)
        installed = lifecycle.rollback(
            engine,
            store,
            layout,
            key,
            version.dist,
            audit=_lifecycle_audit("plugin.rollback", key),
        )
    except (lifecycle.PluginLifecycleError, guard.PluginInstallError) as exc:
        code = getattr(exc, "code", "rollback_failed")
        detail = getattr(exc, "detail", str(exc))
        _audited(resolved, "plugin.rollback_refused", f"{key}:{code}")
        return _fail(t("plugin.rollback_refused", code=code, detail=detail))
    finally:
        engine.dispose()
    _sync_attestations(resolved)  # another version: the word given for the other one lapses
    print(t("plugin.rolled_back", name=key, version=installed.version))
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
    engine = _engine(resolved)
    revoked = {} if engine is None else lifecycle.revoked_dists(engine, layout)
    if engine is not None:
        engine.dispose()
    for verdict in guard.verdicts(layout, os.environ, revoked=revoked):
        plugin = state.installed[verdict.name]
        status = (
            t("plugin.status_ready")
            if verdict.granted
            else t("plugin.status_disabled", reason=verdict.reason)
        )
        mode = (
            t("plugin.mode_isolated")
            if verdict.mode == guard.MODE_ISOLATED
            else t("plugin.mode_in_process")
        )
        print(t("plugin.row", name=plugin.name, version=plugin.version, mode=mode, status=status))
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
    if USE_READBACK in view.warnings:
        print(t("plugin.use_readback"), file=sys.stderr)
    if LNCRAWL_RIGHTS_WARNING in view.warnings:
        print(t("plugin.lncrawl_rights"), file=sys.stderr)


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


# -- the repositories of signed indexes (M6 L07, EF-SRC-017, D14) ------------------------------


def _repo_store(resolved: ResolvedSettings) -> RepoStore | None:
    engine = _engine(resolved)
    if engine is None:
        return None
    return _store_of(engine, resolved)


def _index_fail(error: PluginIndexError) -> int:
    return _fail(t("plugin.index_refused", code=error.code, detail=error.detail))


def run_index_add(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    store = _repo_store(resolved)
    if store is None:
        return 1
    try:
        record = store.add(args.url, args.pubkey or "")
    except PluginIndexError as exc:
        _audited(resolved, "plugin.index_add_refused", f"{args.url}:{exc.code}")
        return _index_fail(exc)
    if not _audited(resolved, "plugin.index_add", f"{record.url}:{record.key_id}"):
        store.remove(record.url)  # no audit row, no pin
        return 1
    print(t("plugin.index_added", url=record.url, key_id=record.key_id))
    return 0


def run_index_list(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    store = _repo_store(resolved)
    if store is None:
        return 1
    records = store.list()
    if not records:
        print(t("plugin.index_none"))
        return 0
    for record in records:
        expires = "-" if record.expires is None else record.expires.strftime("%Y-%m-%d")
        print(
            t(
                "plugin.index_row",
                url=record.url,
                key_id=record.key_id,
                version=record.last_version,
                expires=expires,
            )
        )
    return 0


def run_index_refresh(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    store = _repo_store(resolved)
    if store is None:
        return 1
    records = store.list()
    if args.url is not None:
        records = [record for record in records if record.url == args.url]
    if not records:
        return _fail(t("plugin.index_refused", code="not_found", detail=""))
    from shelfhold.composition import default_network

    async def _run() -> int:
        fetcher = index_fetcher(default_network())
        try:
            for record in records:
                if not _audited(resolved, "plugin.index_refresh", record.url):  # before, not after
                    return 1
                try:
                    outcome = await store.refresh(record, fetcher)
                except PluginIndexError as exc:
                    _audited(resolved, "plugin.index_refresh_refused", f"{record.url}:{exc.code}")
                    return _index_fail(exc)
                if outcome.applied:
                    print(
                        t(
                            "plugin.index_refreshed",
                            url=record.url,
                            version=outcome.document.version,
                            rotated=int(outcome.rotated),
                        )
                    )
                    if outcome.disabled:  # the running processes keep serving until a restart
                        print(
                            t(
                                "plugin.restart_required",
                                keys=t("status.list_separator").join(outcome.disabled),
                            )
                        )
                else:
                    print(
                        t(
                            "plugin.index_unchanged",
                            url=record.url,
                            version=outcome.document.version,
                        )
                    )
        finally:
            await fetcher.aclose()  # one client for every repository, closed once
        return 0

    return asyncio.run(_run())


def run_index_remove(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    store = _repo_store(resolved)
    if store is None:
        return 1
    if not _audited(resolved, "plugin.index_remove", args.url):  # before the change
        return 1
    try:
        store.remove(args.url)
    except PluginIndexError as exc:
        return _index_fail(exc)
    print(t("plugin.index_removed", url=args.url))
    return 0


@ops_command
def run_migrate_to(args: argparse.Namespace, channel: OpsChannel) -> int:
    """The sources of a plugin move to the replacement its index named (M6 L09b): a reserved
    action through the operating channel, the tombstones asked about every line, and a line
    they hold stays where it is — nothing is recreated for an erased series."""
    key, replacement = guard.normalise_name(args.key), guard.normalise_name(args.replacement)
    report = channel.call("ops.plugin_migrate_to", key=key, replacement=replacement)
    print(
        t(
            "plugin.migrated_to",
            moved=report["moved"],
            name=key,
            replacement=replacement,
        )
    )
    if report["blocked"]:
        print(t("plugin.migration_blocked", blocked=report["blocked"]))
    return 0
