"""Public facade of plugin_host (ENF-LEG-001).

A fresh install has no plugin repository and no active source; connected
services and metadata providers ship inactive. The values come from the real
settings (``plugin_repos`` and ``active_sources``, both empty by default).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace

import structlog

from shelfhold.config import Settings
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.adapters.meta.lncrawl import LncrawlService
from shelfhold.plugin_host.adapters.native.gutenberg import GutenbergSource
from shelfhold.plugin_host.adapters.native.wikisource import WikisourceSource
from shelfhold.plugin_host.attestation import InstalledVersion, publisher_attested
from shelfhold.plugin_host.gutenberg_rdf import read_rdf
from shelfhold.plugin_host.install import Layout, read_state, verdicts
from shelfhold.plugin_host.native_sources import first_party_native_candidates
from shelfhold.plugin_host.registry import (
    InstalledPlugin,
    PluginCandidate,
    PluginOrigin,
    PluginRecord,
    PluginRegistry,
    PluginStatus,
    third_party_candidates,
)

__all__ = [
    "InstalledVersion",
    "PluginCandidate",
    "PluginOrigin",
    "PluginRecord",
    "PluginRegistry",
    "PluginState",
    "PluginStatus",
    "first_party_candidates",
    "installed_keys",
    "installed_versions",
    "load_plugin_state",
    "publisher_attested",
    "start_plugins",
]

_LOADED_STATUSES = frozenset({PluginStatus.LOADED, PluginStatus.LOADED_SHIM})


@dataclass(frozen=True)
class PluginState:
    plugin_repos: tuple[str, ...]
    active_sources: tuple[str, ...]
    active_meta_sources: tuple[str, ...]


def load_plugin_state(settings: Settings) -> PluginState:
    """Plugin state derived from the resolved settings."""
    # No metadata provider is active unless it is explicitly listed as an active source.
    return PluginState(
        plugin_repos=settings.plugin_repos,
        active_sources=settings.active_sources,
        active_meta_sources=(),
    )


def first_party_candidates() -> list[PluginCandidate]:
    """The sources the host ships (native, and the connected service of the user's own lncrawl):
    loaded at start, switched on only by the admin
    (`active_sources`). Their code is the host's own and is imported here, not discovered. A source
    that reads XML is given the host's reader: a native adapter has no door to it of its own."""

    def gutenberg() -> GutenbergSource:
        return GutenbergSource(rdf=read_rdf)

    def wikisource() -> WikisourceSource:
        return WikisourceSource()

    def lncrawl() -> LncrawlService:
        return LncrawlService()  # the address is the admin's own, from the settings of the source

    return [
        PluginCandidate(key="gutenberg", origin=PluginOrigin.FIRST_PARTY, load=gutenberg),
        PluginCandidate(key="wikisource", origin=PluginOrigin.FIRST_PARTY, load=wikisource),
        PluginCandidate(key="lncrawl", origin=PluginOrigin.FIRST_PARTY, load=lncrawl),
    ]


def start_plugins(
    settings: Settings, environ: Mapping[str, str] | None = None
) -> tuple[PluginRegistry, list[PluginRecord]]:
    """The one place where third-party code can be loaded, once, at start-up (EF-SRC-019).

    A plugin loads in the host process only in the in-process mode, with
    ``SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1``, its typed confirmation and unchanged files; an
    isolated plugin (the default, D13) is never imported here, whatever the environment allows:
    the companion runs it. Otherwise the plugin is reported as disabled with the reason and never
    imported. The loader is handed nothing: no settings, no secret, no host object.
    """
    layout = Layout.of(settings.data_dir)
    state = read_state(layout)
    judged = verdicts(layout, os.environ if environ is None else dict(environ))
    granted: list[str] = []
    blocked: dict[str, str] = {}
    for verdict in judged:
        if verdict.loads_in_process:
            granted.append(verdict.name)
        elif verdict.mode == guard.MODE_ISOLATED and verdict.granted:
            # enabled, and still never loaded in the host process: that is what the mode says
            blocked[verdict.name] = "isolated mode: the host process never loads it"
        else:
            blocked[verdict.name] = verdict.reason or "refused"
    installed = [InstalledPlugin(i.name, i.top_levels) for i in state.installed.values()]
    plugin_state = load_plugin_state(settings)
    registry = PluginRegistry(
        active_keys=plugin_state.active_sources, granted_distributions=granted
    )
    candidates = [
        *first_party_candidates(),
        *first_party_native_candidates(),  # shipped with the host, off until enabled
        *third_party_candidates(layout.site, installed, blocked=blocked),
    ]
    # M6 L09b (EF-SRC-030): a plugin a host upgrade left behind is neither imported nor migrated
    # — the judgement is read before anything loads, and the candidate is blocked with its reason
    for key, reason in _judged_against_the_host(settings).items():
        candidates = [
            replace(c, blocked=f"{reason}: this host cannot run the plugin") if c.key == key else c
            for c in candidates
        ]
    records = registry.load_all(candidates)
    _judge_against_the_host(settings, registry, records)
    log = structlog.get_logger()
    for record in registry.records:  # re-read: the judgement may have marked records
        log.info(
            "plugin.third_party", key=record.key, status=record.status.value, reason=record.reason
        )
    return registry, records


def _judged_against_the_host(settings: Settings) -> dict[str, str]:
    """The installed plugins a host upgrade left behind, each with its closed reason. Nothing is
    judged without the host's database: no engine, no judgement."""
    database = getattr(settings, "database_path", None)  # a caller may know only the data dir
    if database is None or not database.exists():
        return {}
    from shelfhold_sdk.models import API_VERSION

    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.plugin_host import lifecycle

    engine = create_db_engine(database)
    try:
        return lifecycle.incompatible_installed(
            engine, host_version=lifecycle.HOST_VERSION, api_version=API_VERSION
        )
    finally:
        engine.dispose()


def _judge_against_the_host(
    settings: Settings, registry: PluginRegistry, records: list[PluginRecord]
) -> None:
    """What a host upgrade leaves behind, judged at start-up (EF-SRC-030): an installed plugin
    out of `min_host` or `api` is marked incompatible, and the state a loaded third-party
    plugin cannot read yet is migrated by its own `migrate_state` before its first call — a
    plugin whose state cannot be migrated is marked, never called, and the start-up itself goes
    on. Nothing is judged without the host's database: no engine, no judgement."""
    database = getattr(settings, "database_path", None)  # a caller may know only the data dir
    if database is None or not database.exists():
        return
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.plugin_host import state

    engine = create_db_engine(database)
    try:
        for key, reason in _judged_against_the_host(settings).items():
            registry.mark_incompatible(key, f"{reason}: this host cannot run the plugin")
        versions = installed_versions(settings)
        for record in records:
            # the registry is re-read for each key: marking a plugin incompatible above may
            # have replaced the record this list still holds, and a plugin so marked is never
            # migrated — its code is not called at all
            current = registry.get(record.key) or record
            if (
                current.origin is not PluginOrigin.THIRD_PARTY
                or current.status not in _LOADED_STATUSES
                or current.manifest is None
                or current.instance is None
            ):
                continue
            version = versions.get(current.key)
            if version is None:
                continue
            try:
                state.migrate_documents(engine, current.key, current.instance, version.version)
            except state.PluginStateError as exc:
                registry.mark_incompatible(current.key, f"state {exc.code}: {exc.detail}")
    finally:
        engine.dispose()


def installed_keys(layout: Layout) -> frozenset[str]:
    """The plugin keys the installed third-party plugins declare, by their entry points. Nothing
    is imported: the distributions the guard recorded are read from the third-party directory
    alone. A key is how an index (and an attestation) names a plugin; a distribution name is how
    the guard does — this is the bridge between the two. The discovery stays behind the start-up
    facade: no other module reaches the registry's loader (ENF-SEC-009)."""
    state = read_state(layout)
    if not state.installed:
        return frozenset()
    installed = [InstalledPlugin(i.name, i.top_levels) for i in state.installed.values()]
    return frozenset(c.key for c in third_party_candidates(layout.site, installed, blocked={}))


def installed_versions(settings: Settings) -> dict[str, InstalledVersion]:
    """The third-party plugins the guard installed, by plugin key, with the installed version and
    files digest (EF-EXP-065: what an attestation is bound to). Nothing is imported."""
    layout = Layout.of(settings.data_dir)
    state = read_state(layout)
    if not state.installed:
        return {}
    installed = [InstalledPlugin(i.name, i.top_levels) for i in state.installed.values()]
    found: dict[str, InstalledVersion] = {}
    for candidate in third_party_candidates(layout.site, installed, blocked={}):
        plugin = state.installed.get(candidate.dist or "")
        if plugin is not None:
            found[candidate.key] = InstalledVersion(
                candidate.key, plugin.name, plugin.version, plugin.digest
            )
    return found
