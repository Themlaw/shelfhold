"""M6 L09b, adversarial review PoC (red): what the state migrations, the host upgrades and the
`replaced_by` migration let through.

- a `plugin migrate-to` seals nothing: the erased series, and a line held back by its inverse
  alias, are not blocked under the replacing plugin — the replacement acquires them again;
- `plugin migrate-to` moves the sources to any key, one the index never named or one it revoked;
- the index accepts a `replaced_by` that names the entry itself or a revoked key, and a
  `min_host` that is not a version (it compares below every host and is never refused);
- a `migrate_state` that raises, or answers nothing, is not a refused migration: the error
  leaves the start-up, and an empty answer is no closed refusal;
- a plugin marked incompatible by `min_host` still has its `migrate_state` called;
- a `manylinux` wheel is flagged « ABI incompatible » on the Linux interpreter it matches.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.public import SeriesBindingRef
from shelfhold.ops.plugin_migration import PluginMigrationOps
from shelfhold.ops.source_migration import SourceMigrationOps
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host import public, state
from shelfhold.plugin_host.index.document import parse_document
from shelfhold.plugin_host.index.minisign import PluginIndexError
from shelfhold.plugin_host.registry import (
    PluginOrigin,
    PluginRecord,
    PluginRegistry,
    PluginStatus,
)
from tests.integration.test_l27_source_aliases import GONE, PLUGIN, SOURCE, alias, erase, live
from tests.l50_env import seeded
from tests.plugin_index_env import index_document, plugin_entry

ADMIN = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)
REPLACEMENT = "plugin-b"
KEY = "acme"


def _expires() -> str:
    return (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- replaced_by through the tombstones ----------------------------------------------------------


@pytest.mark.req("EF-BIB-054", "EF-SRC-030")
def test_poc_migrate_to_leaves_erased_series_open_under_the_replacement(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """After the move, the replacing plugin asks the tombstones under its own key
    (`SeriesBindingRef(plugin, source, remote)`): the erased series and the line its inverse
    alias held back must stay blocked under that key, or the replacement brings them back."""
    with seeded(settings, tmp_path / "inst") as env:
        gone = live(env, GONE)
        live(env, "keep-1")
        live(env, "hit-1")
        alias(env, "hit-1", GONE)
        erase(env, monkeypatch, settings, gone)

        PluginMigrationOps(env.inst.comp).migrate_to(
            key=PLUGIN, replacement=REPLACEMENT, principal=ADMIN
        )
        service = SourceMigrationOps(env.inst.comp)._service()
        assert service.check(SeriesBindingRef(REPLACEMENT, SOURCE, GONE), root_id=None).blocked
        assert service.check(SeriesBindingRef(REPLACEMENT, SOURCE, "hit-1"), root_id=None).blocked


@pytest.mark.req("EF-BIB-054", "EF-SRC-030")
def test_poc_migrate_to_moves_sources_to_a_key_no_index_named(
    settings: Settings, tmp_path: Path
) -> None:
    """No index names `plugin-b` as the replacement of `plugin-a` (there is no index at all):
    the move is refused and nothing changes."""
    with seeded(settings, tmp_path / "inst") as env:
        live(env, "keep-1")
        with pytest.raises(Exception):  # noqa: B017 - any closed refusal will do
            PluginMigrationOps(env.inst.comp).migrate_to(
                key=PLUGIN, replacement="never-named", principal=ADMIN
            )
        with env.inst.engine.connect() as connection:
            owners = set(
                connection.execute(sa.select(acquisition.source_item.c.plugin_id)).scalars()
            )
        assert owners == {PLUGIN}


# -- the index fields ----------------------------------------------------------------------------


@pytest.mark.req("EF-BIB-054", "EF-SRC-030")
@pytest.mark.parametrize("revoked", [False, True], ids=["itself", "revoked_key"])
def test_poc_index_accepts_replaced_by_itself_or_a_revoked_key(revoked: bool) -> None:
    target = "gone-key" if revoked else KEY
    document = index_document(
        version=1,
        expires=_expires(),
        plugins=[plugin_entry(key=KEY, deprecated=True, replaced_by=target)],
        plugin_keys=["gone-key"] if revoked else [],
    )
    with pytest.raises(PluginIndexError):
        parse_document(document)


@pytest.mark.req("EF-SRC-030")
@pytest.mark.parametrize("min_host", ["latest", "v9.0.0", "9.x"])
def test_poc_index_accepts_a_min_host_that_is_not_a_version(min_host: str) -> None:
    """A `min_host` that is not a dotted version compares below every host (`version_key`):
    it would never refuse anything. The document is refused instead."""
    document = index_document(
        version=1, expires=_expires(), plugins=[plugin_entry(key=KEY, min_host=min_host)]
    )
    with pytest.raises(PluginIndexError):
        parse_document(document)


# -- migrate_state -------------------------------------------------------------------------------


def _engine_with_a_document(settings: Settings) -> sa.Engine:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.plugin_state).values(
                plugin_id=KEY, key="cursor", payload={"page": 1}, payload_schema=1
            )
        )
    return engine


class _Raises:
    payload_schema = 2

    def migrate_state(self, from_version: int, key: str, payload: Any) -> Any:
        raise RuntimeError("the plugin's own bug")


class _Nothing:
    payload_schema = 2

    def migrate_state(self, from_version: int, key: str, payload: Any) -> Any:
        return None


@pytest.mark.req("EF-SRC-030")
@pytest.mark.parametrize("plugin", [_Raises, _Nothing], ids=["raises", "answers_nothing"])
def test_poc_a_failing_migrate_state_is_not_a_closed_refusal(
    settings: Settings, plugin: type
) -> None:
    """The plugin is untrusted: its error, or an empty answer, is a refused migration
    (`PluginStateError`), and the state stays exactly as it was."""
    engine = _engine_with_a_document(settings)
    try:
        with pytest.raises(state.PluginStateError):
            state.migrate_documents(engine, KEY, plugin(), "2.0.0")
        assert state.read_documents(engine, KEY) == [
            state.StateDocument("cursor", {"page": 1}, 1, None)
        ]
    finally:
        engine.dispose()


def _judge(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    instance: Any,
    judged: dict[str, str],
) -> PluginRegistry:
    from shelfhold.plugin_host import lifecycle

    monkeypatch.setattr(lifecycle, "incompatible_installed", lambda *_a, **_k: dict(judged))
    monkeypatch.setattr(
        public, "installed_versions", lambda _s: {KEY: SimpleNamespace(version="2.0.0")}
    )
    record = PluginRecord(
        KEY, PluginOrigin.THIRD_PARTY, PluginStatus.LOADED, None, object(), instance
    )
    registry = PluginRegistry()
    registry._records = [record]
    public._judge_against_the_host(settings, registry, [record])
    return registry


@pytest.mark.req("EF-SRC-030")
def test_poc_a_raising_migrate_state_stops_the_start_up(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A plugin whose hook raises is marked, never called — the host itself starts."""
    _engine_with_a_document(settings).dispose()
    registry = _judge(monkeypatch, settings, _Raises(), {})
    marked = registry.get(KEY)
    assert marked is not None and marked.status is PluginStatus.INCOMPATIBLE


@pytest.mark.req("EF-SRC-030")
def test_poc_migrate_state_called_on_a_plugin_marked_incompatible(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """`min_host` marked the plugin incompatible: none of its code is called afterwards, its
    `migrate_state` neither."""
    _engine_with_a_document(settings).dispose()
    calls: list[int] = []

    class Hook:
        payload_schema = 2

        def migrate_state(self, from_version: int, key: str, payload: Any) -> Any:
            calls.append(from_version)
            return payload

    _judge(monkeypatch, settings, Hook(), {KEY: "min_host"})
    assert calls == []
    engine = create_db_engine(settings.database_path)
    try:
        assert [d.payload_schema for d in state.read_documents(engine, KEY)] == [1]
    finally:
        engine.dispose()


# -- ABI -----------------------------------------------------------------------------------------


@pytest.mark.req("ENF-OPS-018")
def test_poc_manylinux_wheel_flagged_on_the_linux_interpreter_it_matches() -> None:
    """`sysconfig.get_platform()` says `linux_x86_64`; a Linux wheel says `manylinux_*` or
    `musllinux_*` (PEP 600/656), never `linux_x86_64`: the tag matches this interpreter."""
    linux = guard.Interpreter(python="3.13", abi="cp313", platform="linux_x86_64")
    assert guard.abi_problem(["cp313-cp313-manylinux_2_17_x86_64"], linux) is None
    assert guard.abi_problem(["cp313-abi3-manylinux2014_x86_64"], linux) is None


# -- counter-review: the seal of a cut migration, the family that is no index's word ------------


def _named(env: Any, replacement: str) -> None:
    """The install row an index wrote for `plugin-a`: its entry named `replacement`."""
    with env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.plugin_install).values(
                plugin_id=PLUGIN, version="1.0.0", api="2", origin="local",
                sha256="sha256:" + "a" * 64, index_url="https://index.example.org/index.json",
                signer_pubkey="k", min_host="1.0.0", replaced_by=replacement,
            )
        )  # fmt: skip


@pytest.mark.req("EF-BIB-054", "EF-SRC-030")
def test_poc_a_migration_cut_before_its_seal_never_seals_again(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Every living line moved, then the process is cut before the seal (the rows are
    committed, the tombstones under the replacing key are not). The rerun the module promises
    must finish the job: the erased series stays blocked under the replacement."""
    with seeded(settings, tmp_path / "inst") as env:
        gone = live(env, GONE)
        live(env, "keep-1")
        erase(env, monkeypatch, settings, gone)
        _named(env, REPLACEMENT)

        def cut(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the process is cut after the commit")

        ops = PluginMigrationOps(env.inst.comp)
        with monkeypatch.context() as patch:
            patch.setattr(PluginMigrationOps, "_seal", cut)
            with pytest.raises(RuntimeError):
                ops.migrate_to(key=PLUGIN, replacement=REPLACEMENT, principal=ADMIN)
        with contextlib.suppress(Exception):  # the rerun; judged by what stays blocked
            PluginMigrationOps(env.inst.comp).migrate_to(
                key=PLUGIN, replacement=REPLACEMENT, principal=ADMIN
            )
        service = SourceMigrationOps(env.inst.comp)._service()
        assert service.check(SeriesBindingRef(REPLACEMENT, SOURCE, GONE), root_id=None).blocked


@pytest.mark.req("EF-BIB-054", "EF-SRC-030")
def test_poc_a_plugin_no_index_named_moves_to_any_key_of_its_family(
    settings: Settings, tmp_path: Path
) -> None:
    """`plugin-a` was never installed from an index: no `replaced_by` was ever said for it. A
    key that only shares its first segment is no index's word either: refused, nothing moves."""
    with seeded(settings, tmp_path / "inst") as env:
        live(env, "keep-1")
        with pytest.raises(Exception):  # noqa: B017 - any closed refusal will do
            PluginMigrationOps(env.inst.comp).migrate_to(
                key=PLUGIN, replacement="plugin-anyone", principal=ADMIN
            )
        with env.inst.engine.connect() as connection:
            owners = set(
                connection.execute(sa.select(acquisition.source_item.c.plugin_id)).scalars()
            )
        assert owners == {PLUGIN}
