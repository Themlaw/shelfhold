"""M6 L09b (EF-SRC-030, ENF-OPS-018, D16): plugin state migrations and host upgrades.

A plugin's state documents are versioned (`payload_schema`): a version that changes the schema
ships `migrate_state(from_version)`, and the conformance kit fails a plugin that changes the
schema without it. The host calls the hook before the first call of a new version, and the
rollback is refused, with the reason, toward a version that cannot read the state the installed
version wrote (`state_written_by_version`). After a host upgrade a plugin out of `min_host` or
out of `api` is marked and `plugin.incompatible` is told once; at start-up the wheel tags are
compared to the interpreter and an ABI-incompatible plugin is flagged, never executed, with a
guided reinstall (the « Python N+1 » policy of docs/admin/python-et-plugins.md). The index
fields `deprecated`, `replaced_by` and `min_host` are read: a plugin whose `min_host` exceeds
the host is refused, a deprecated plugin says so, and `replaced_by` never installs the
replacement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from shelfhold_sdk.testing.fakes import FakeRangedConnectedService, fake_ranged_fetcher
from shelfhold_sdk.testing.kit import run_source_conformance

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host import lifecycle, state
from shelfhold.plugin_host.public import start_plugins
from shelfhold.plugin_host.registry import PluginStatus
from tests.auth_env import run_cli
from tests.plugin_guard_env import Wheel, build_wheel
from tests.plugin_index_env import (
    FakeFetcher,
    IndexKey,
    index_document,
    make_key,
    plugin_entry,
    sign_index,
    trusted_comment,
)

pytestmark = pytest.mark.req("EF-SRC-030")

URL = "https://index.example.org/index.json"
WHEEL_BASE = "https://index.example.org/wheels/"
DIST = "acme-plug"
KEY = "acme"
MODULE = "acme_plug"


def _expires(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wheel(
    tmp_path: Path, version: str, *, dist: str = DIST, key: str = KEY, module: str = MODULE
) -> Any:
    return build_wheel(
        tmp_path,
        dist,
        {f"{module}/__init__.py": f"x = {version!r}\n"},
        entry=(key, f"{module}:plugin"),
        version=version,
    )


def _served(
    fetcher: FakeFetcher,
    key: IndexKey,
    *,
    version: int,
    wheel: Any,
    plugin_version: str,
    url: str = URL,
    **over: Any,
) -> None:
    """Serve a signed index that vouches for the wheel, and the wheel itself."""
    wheel_url = WHEEL_BASE + wheel.path.name
    fetcher.wheels[wheel_url] = wheel.path.read_bytes()
    expires = _expires()
    document = index_document(
        version=version,
        expires=expires,
        plugins=[plugin_entry(key=KEY, version=plugin_version,
                              sha256="sha256:" + wheel.sha256, wheel_url=wheel_url, **over)],
    )  # fmt: skip
    fetcher.serving(
        url, document, sign_index(document, key, comment=trusted_comment(version, expires))
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    fetcher: FakeFetcher,
    *,
    url: str = URL,
) -> int:
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: fetcher)
    return run_cli(monkeypatch, settings, "plugin", "install", KEY, "--index", url)


def _state_version(settings: Settings, dist: str = DIST) -> str | None:
    state_path = settings.data_dir / "plugins" / "state.json"
    if not state_path.exists():
        return None
    installed = json.loads(state_path.read_text(encoding="utf-8")).get("installed", {})
    return installed.get(dist, {}).get("version")


def _tagged_wheel(tmp_path: Path, version: str, tags: tuple[str, ...]) -> Wheel:
    """A wheel whose WHEEL metadata declares the given PEP 425 tags (the filename follows)."""
    base = DIST.replace("-", "_")
    info = f"{base}-{version}.dist-info"
    files: dict[str, str] = {
        f"{MODULE}/__init__.py": "x = 1\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\n"
        + "".join(f"Tag: {tag}\n" for tag in tags),
        f"{info}/entry_points.txt": f"[shelfhold.plugins]\n{KEY} = {MODULE}:plugin\n",
    }
    files[f"{info}/RECORD"] = "".join(f"{n},,\n" for n in [*files, f"{info}/RECORD"])
    path = tmp_path / f"{base}-{version}-{tags[0]}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return Wheel(path, hashlib.sha256(path.read_bytes()).hexdigest())


def _seed_state(settings: Settings, *, schema: int, written_by: str) -> None:
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.plugin_state).values(
                    plugin_id=KEY, key="cursor", payload={"page": 1}, payload_schema=schema,
                    state_written_by_version=written_by,
                )
            )  # fmt: skip
    finally:
        engine.dispose()


def _events(settings: Settings, event_type: str) -> list[tuple[str, str]]:
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.select(platform.event.c.type, platform.event.c.payload).where(
                    platform.event.c.type == event_type
                )
            ).all()
    finally:
        engine.dispose()
    return [(r.type, json.dumps(r.payload, sort_keys=True)) for r in rows]


# -- the kit fails a plugin that changes payload_schema without migrate_state -------------------


class _NoHook(FakeRangedConnectedService):
    payload_schema = 2  # the schema changed from 1 to 2, and no migrate_state ships


class _WithHook(_NoHook):
    def migrate_state(self, from_version: int, key: str, payload: Any) -> Any:
        return payload


@pytest.mark.req("EF-SRC-030", "ENF-QUAL-003")
def test_plugin_state_migration_required_when_payload_schema_changes() -> None:
    """A plugin whose `payload_schema` passes from 1 to 2 without `migrate_state(from_version)`
    fails the conformance kit; with the hook, the check passes."""
    service = _NoHook()
    report = asyncio.run(
        run_source_conformance(service, fake_ranged_fetcher(), reinstall=service.reinstall)
    )
    failed = [check.name for check in report.failures]
    assert "state_migration_declared" in failed

    fixed = _WithHook()
    report = asyncio.run(
        run_source_conformance(fixed, fake_ranged_fetcher(), reinstall=fixed.reinstall)
    )
    assert not [c for c in report.failures if c.name == "state_migration_declared"]

    unchanged = FakeRangedConnectedService()  # schema 1, the one every plugin starts with
    report = asyncio.run(
        run_source_conformance(unchanged, fake_ranged_fetcher(), reinstall=unchanged.reinstall)
    )
    assert not [c for c in report.failures if c.name == "state_migration_declared"]


# -- the host calls migrate_state before the first call of a new version -------------------------


@pytest.mark.req("EF-SRC-030")
def test_host_calls_migrate_state_before_the_first_call_of_a_new_version(
    settings: Settings,
) -> None:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.plugin_state).values(
                    plugin_id=KEY, key="cursor", payload={"page": 1}, payload_schema=1
                )
            )
        seen: list[tuple[int, str, Any]] = []

        class NewVersion:
            payload_schema = 2

            def migrate_state(self, from_version: int, key: str, payload: Any) -> Any:
                seen.append((from_version, key, payload))
                return {**payload, "v": 2}

        assert state.migrate_documents(engine, KEY, NewVersion(), "2.0.0") == 1
        assert seen == [(1, "cursor", {"page": 1})]
        assert state.read_documents(engine, KEY) == [
            state.StateDocument("cursor", {"page": 1, "v": 2}, 2, "2.0.0")
        ]

        class NoHook:
            payload_schema = 3  # the schema changes again, and no hook ships this time

        with pytest.raises(state.PluginStateError) as raised:
            state.migrate_documents(engine, KEY, NoHook(), "3.0.0")
        assert raised.value.code == "migration_missing"

        class Older:
            payload_schema = 1  # a version that cannot read the state the new one wrote

        with pytest.raises(state.PluginStateError) as raised:
            state.migrate_documents(engine, KEY, Older(), "0.9.0")
        assert raised.value.code == "state_newer"
    finally:
        engine.dispose()


@pytest.mark.req("EF-SRC-030")
def test_migrate_state_answering_an_unkeepable_payload_is_a_closed_refusal(
    settings: Settings,
) -> None:
    """The hook answers a set: the host keeps a payload as the JSON column takes it, and an
    answer it cannot keep is the plugin's own bug (`hook_failed`), never the host's crash —
    the start-up that catches the refusal keeps going, and the state stays as it was."""
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.plugin_state).values(
                    plugin_id=KEY, key="cursor", payload={"page": 1}, payload_schema=1
                )
            )

        class Unkeepable:
            payload_schema = 2

            def migrate_state(self, from_version: int, key: str, payload: Any) -> Any:
                return {"pages"}  # a set: no JSON document keeps it

        with pytest.raises(state.PluginStateError) as raised:
            state.migrate_documents(engine, KEY, Unkeepable(), "2.0.0")
        assert raised.value.code == "hook_failed"
        assert state.read_documents(engine, KEY) == [
            state.StateDocument("cursor", {"page": 1}, 1, None)
        ]
    finally:
        engine.dispose()


# -- the rollback is refused toward a version that cannot read the written state ------------------


@pytest.mark.req("EF-SRC-030", "EF-SRC-020")
def test_rollback_refused_when_persisted_state_newer(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    signer = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", signer.public_b64)
        == 0
    )
    first, second = _wheel(tmp_path, "1.0.0"), _wheel(tmp_path, "2.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: newer)
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) == 0
    assert _state_version(settings) == "2.0.0"

    # the state the installed version wrote: the rollback's target cannot read it
    _seed_state(settings, schema=2, written_by="2.0.0")
    assert run_cli(monkeypatch, settings, "plugin", "rollback", KEY) != 0
    assert _state_version(settings) == "2.0.0"  # nothing moved
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            audit = connection.execute(
                sa.text("SELECT action, target FROM audit WHERE action LIKE 'plugin.rollback%'")
            ).all()
    finally:
        engine.dispose()
    refusals = [(r.action, r.target) for r in audit]
    assert ("plugin.rollback_refused", f"{KEY}:state_newer") in refusals

    # the state of the version the rollback returns to: no refusal
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            connection.execute(sa.delete(acquisition.plugin_state))
    finally:
        engine.dispose()
    assert run_cli(monkeypatch, settings, "plugin", "rollback", KEY) == 0
    assert _state_version(settings) == "1.0.0"


# -- a plugin out of min_host or api after a host upgrade is marked, the event is told once ------


@pytest.mark.req("EF-SRC-030")
def test_plugin_incompatible_event_after_host_upgrade(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.infrastructure.clock import SystemClock
    from shelfhold.integrations.apprise import operating_bus

    migrate(settings)
    signer = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", signer.public_b64)
        == 0
    )
    wheel = _wheel(tmp_path, "1.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=wheel, plugin_version="1.0.0", min_host="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0

    engine = create_db_engine(settings.database_path)
    try:
        # the fields the index entry declared are the row's: the judgement of a later host
        with engine.connect() as connection:
            row = connection.execute(
                sa.select(acquisition.plugin_install.c.api, acquisition.plugin_install.c.min_host)
            ).one()
        assert (row.api, row.min_host) == ("2", "1.0.0")

        # this host runs them: nothing is marked
        assert lifecycle.incompatible_installed(engine, host_version="1.1.0", api_version=2) == {}

        # the host was upgraded: the plugin's api is out of the N/N-1 window now
        with engine.begin() as connection:
            connection.execute(sa.update(acquisition.plugin_install).values(api="3"))
        judged = lifecycle.incompatible_installed(engine, host_version="1.1.0", api_version=2)
        assert judged == {KEY: "api"}

        bus = operating_bus(SystemClock())
        assert lifecycle.announce_incompatible(engine, bus, judged) == [KEY]
        assert _events(settings, "plugin.incompatible") == [
            ("plugin.incompatible", json.dumps({"plugin": KEY, "reason": "api"}, sort_keys=True))
        ]
        assert lifecycle.announce_incompatible(engine, bus, judged) == []  # told once
        assert len(_events(settings, "plugin.incompatible")) == 1

        # another host upgrade, another reason: told once for that reason too
        with engine.begin() as connection:
            connection.execute(
                sa.update(acquisition.plugin_install).values(api="2", min_host="9.9.0")
            )
        judged = lifecycle.incompatible_installed(engine, host_version="1.1.0", api_version=2)
        assert judged == {KEY: "min_host"}
        assert lifecycle.announce_incompatible(engine, bus, judged) == [KEY]
        assert len(_events(settings, "plugin.incompatible")) == 2
    finally:
        engine.dispose()

    # the start-up load marks the plugin: never loaded, the reason kept
    registry, _records = start_plugins(settings)
    record = registry.get(KEY)
    assert record is not None
    assert record.status is PluginStatus.INCOMPATIBLE
    assert "min_host" in (record.reason or "")


# -- an ABI-incompatible wheel is flagged after a Python upgrade, never executed ------------------


@pytest.mark.req("ENF-OPS-018")
def test_abi_incompatible_plugin_flagged_after_python_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    migrate(settings)
    after_upgrade = guard.Interpreter(python="3.13", abi="cp313", platform="linux_x86_64")
    assert guard.abi_problem(("cp312-none-any",), after_upgrade) is not None
    assert guard.abi_problem(("py3-none-any",), after_upgrade) is None

    # a wheel installed under Python 3.12, then the host moves to 3.13
    stale = _tagged_wheel(tmp_path, "1.0.0", ("cp312-none-any",))
    assert (
        run_cli(
            monkeypatch, settings, "plugin", "install", str(stale.path), "--sha256", stale.sha256
        )
        == 0
    )
    pure = _wheel(tmp_path, "2.0.0", dist="beta-plug", key="beta", module="beta_plug")
    assert (
        run_cli(monkeypatch, settings, "plugin", "install", str(pure.path), "--sha256", pure.sha256)
        == 0
    )
    capsys.readouterr()  # the installs said their own words; only the list's are judged below

    layout = guard.Layout.of(settings.data_dir)
    verdicts = {v.name: v for v in guard.verdicts(layout, {}, interpreter=after_upgrade)}
    assert not verdicts[DIST].granted
    assert "ABI incompatible" in (verdicts[DIST].reason or "")
    assert "ABI incompatible" not in (verdicts["beta-plug"].reason or "")

    assert run_cli(monkeypatch, settings, "plugin", "list") == 0
    out = capsys.readouterr().out
    flagged = [line for line in out.splitlines() if DIST in line]
    assert flagged and all("ABI incompatible" in line for line in flagged)
    assert "shelfhold plugin install" in out  # the guided reinstall

    # the policy is written (ENF-OPS-018): Python N+1, one year before the end of life of N
    policy = Path(__file__).resolve().parents[3] / "docs" / "admin" / "python-et-plugins.md"
    assert policy.exists()
    assert "N+1" in policy.read_text(encoding="utf-8")


# -- the index fields deprecated, replaced_by and min_host are read -------------------------------


@pytest.mark.req("EF-SRC-030")
def test_index_fields_deprecated_replaced_by_min_host(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    migrate(settings)
    signer = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", signer.public_b64)
        == 0
    )
    wheel = _wheel(tmp_path, "1.0.0")

    # min_host above the host version: the install is refused, nothing is installed
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=wheel, plugin_version="1.0.0", min_host="9.9.0")
    assert _install(monkeypatch, settings, fetcher) != 0
    assert _state_version(settings) is None
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            audit = connection.execute(
                sa.text("SELECT action, target FROM audit WHERE action = 'plugin.install_refused'")
            ).all()
    finally:
        engine.dispose()
    assert (f"{KEY}:min_host",) in [(r.target,) for r in audit]

    # deprecated, with a named replacement: installed, said, and the replacement is not installed
    served = FakeFetcher()
    _served(
        served, signer, version=2, wheel=wheel, plugin_version="1.0.0",
        min_host="1.0.0", deprecated=True, replaced_by="acme2",
    )  # fmt: skip
    assert _install(monkeypatch, settings, served) == 0
    out = capsys.readouterr().out
    assert "déprécié" in out
    assert "acme2" in out
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                sa.select(acquisition.plugin_install.c.api, acquisition.plugin_install.c.min_host)
            ).one()
        assert (row.api, row.min_host) == ("2", "1.0.0")
    finally:
        engine.dispose()
    installed = json.loads(
        (settings.data_dir / "plugins" / "state.json").read_text(encoding="utf-8")
    )["installed"]
    assert list(installed) == [DIST]  # replaced_by never installs the replacement

    # the update reads the fields of the new entry too
    second = _wheel(tmp_path, "2.0.0")
    deprecated = FakeFetcher()
    _served(
        deprecated, signer, version=3, wheel=second, plugin_version="2.0.0",
        min_host="1.0.0", deprecated=True, replaced_by="acme2",
    )  # fmt: skip
    monkeypatch.setattr(
        "shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: deprecated
    )
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) == 0
    out = capsys.readouterr().out
    assert "déprécié" in out
    assert _state_version(settings) == "2.0.0"
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            replaced_by = connection.execute(
                sa.select(acquisition.plugin_install.c.replaced_by)
            ).scalar()
        assert replaced_by == "acme2"  # the update keeps the new entry's word too
    finally:
        engine.dispose()


def test_index_refresh_keeps_the_replaced_by_word_of_the_installed_plugin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The index deprecates the installed version, then names another successor: the install
    row keeps the index's own word — a migration moves by it, never by a stale one, and the
    plugin itself is untouched by the refresh."""
    migrate(settings)
    signer = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", signer.public_b64)
        == 0
    )
    wheel = _wheel(tmp_path, "1.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=wheel, plugin_version="1.0.0", min_host="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0

    def _replaced_by(settings: Settings) -> str | None:
        engine = create_db_engine(settings.database_path)
        try:
            with engine.connect() as connection:
                return connection.execute(
                    sa.select(acquisition.plugin_install.c.replaced_by)
                ).scalar()
        finally:
            engine.dispose()

    assert _replaced_by(settings) is None

    # the index deprecates the installed version: the refresh keeps the word, the plugin
    # itself is untouched
    deprecated = FakeFetcher()
    _served(
        deprecated, signer, version=2, wheel=wheel, plugin_version="1.0.0",
        min_host="1.0.0", deprecated=True, replaced_by="acme2",
    )  # fmt: skip
    monkeypatch.setattr(
        "shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: deprecated
    )
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0
    assert _replaced_by(settings) == "acme2"
    assert _state_version(settings) == "1.0.0"

    # a later index names another successor: the word follows, the install still untouched
    successor = FakeFetcher()
    _served(
        successor, signer, version=3, wheel=wheel, plugin_version="1.0.0",
        min_host="1.0.0", deprecated=True, replaced_by="acme3",
    )  # fmt: skip
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: successor)
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0
    assert _replaced_by(settings) == "acme3"
    assert _state_version(settings) == "1.0.0"


@pytest.mark.req("EF-SRC-030", "EF-BIB-054")
def test_index_pinned_again_to_another_key_names_no_successor_for_the_previous_installs(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The same address pinned again to another key is another signer's index (L08): its word
    reaches only the installs it made, never one the previous key vouched for — a refresh that
    names a successor leaves the earlier install's row as it was."""
    migrate(settings)
    first = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", first.public_b64)
        == 0
    )
    wheel = _wheel(tmp_path, "1.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, first, version=1, wheel=wheel, plugin_version="1.0.0", min_host="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0

    other = make_key()
    assert run_cli(monkeypatch, settings, "plugin", "index", "remove", URL) == 0
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", other.public_b64)
        == 0
    )
    hostile = FakeFetcher()
    _served(
        hostile, other, version=2, wheel=wheel, plugin_version="1.0.0",
        min_host="1.0.0", deprecated=True, replaced_by="evil",
    )  # fmt: skip
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: hostile)
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0

    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    acquisition.plugin_install.c.signer_pubkey,
                    acquisition.plugin_install.c.replaced_by,
                )
            ).one()
    finally:
        engine.dispose()
    assert row.replaced_by is None
    assert row.signer_pubkey != other.public_b64
