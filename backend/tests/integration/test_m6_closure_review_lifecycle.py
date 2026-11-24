"""M6 contre-relecture B (cycle de vie des plugins) : PoC rouges sur la tête du jalon.

1. une roue donnée à la main après une install signée (même clé) hérite des secrets, des
   réglages et de l'état de l'install signée — sans mise de côté ni confirmation ;
2. une roue donnée à la main (autre distribution, même clé) est acceptée à côté d'une installation
   signée et lui retire son épinglage d'index ;
3. un ``pin`` posé pendant l'``update`` (entre la vérification et l'écriture) est ignoré ;
4. un rollback HTTP dont l'installeur refuse remonte en 500, sans ligne de refus.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host import lifecycle
from shelfhold.plugin_host.index import RepoStore
from shelfhold.plugin_host.settings import SourceSettings, parse_settings_schema
from tests.auth_env import AuthEnv, auth_env, db_rows, run_cli
from tests.integration.test_l08_plugin_signer_pin import SCHEMA
from tests.integration.test_l09a_plugin_lifecycle import (
    DIST,
    KEY,
    URL,
    _install,
    _install_row,
    _served,
    _state_version,
    _wheel,
)
from tests.plugin_guard_env import build_wheel
from tests.plugin_index_env import FakeFetcher, make_key

pytestmark = pytest.mark.req("EF-SRC-017")

SECRET = "secret-value-7f3a51"  # noqa: S105 - synthetic


def _add_index(monkeypatch: pytest.MonkeyPatch, settings: Settings, pubkey: str) -> None:
    assert run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", pubkey) == 0


def _store(settings: Settings) -> tuple[Any, SourceSettings]:
    engine = create_db_engine(settings.database_path)
    return engine, SourceSettings(engine, SecretVault(boot_check(settings, announce=False)))


def _evil(tmp_path: Path) -> Any:
    (tmp_path / "evil").mkdir(exist_ok=True)
    return build_wheel(
        tmp_path / "evil", "evil-plug", {"evil_plug/__init__.py": "x = 1\n"},
        entry=(KEY, "evil_plug:plugin"),
    )  # fmt: skip


def test_hand_wheel_after_signed_install_inherits_its_secrets(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=_wheel(tmp_path, "1.0.0"), plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    engine, store = _store(settings)
    schema = parse_settings_schema(SCHEMA)
    store.set_values(KEY, schema, {"lang": "en"}, actor=Actor("cli:uid1000"))
    store.set_secret(KEY, schema, "api_key", SECRET, actor=Actor("cli:uid1000"))
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0

    evil = _evil(tmp_path)  # code no index signed, declaring the same key
    assert (
        run_cli(monkeypatch, settings, "plugin", "install", str(evil.path), "--sha256", evil.sha256)
        == 0
    )
    try:
        inherited = store.load(KEY).values.get("api_key") == SECRET
        held = [r[0] for r in db_rows(settings, "SELECT plugin_id FROM source_secret")]
    finally:
        engine.dispose()
    assert not inherited, (
        f"the hand wheel reads signer A's secret without confirmation (rows: {held})"
    )


def test_hand_wheel_with_the_same_key_beside_a_signed_install(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=_wheel(tmp_path, "1.0.0"), plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    pin = db_rows(
        settings, "SELECT index_url, signer_pubkey FROM plugin_install WHERE plugin_id = ?", KEY
    )

    evil = _evil(tmp_path)
    code = run_cli(
        monkeypatch, settings, "plugin", "install", str(evil.path), "--sha256", evil.sha256
    )
    after = db_rows(
        settings, "SELECT index_url, signer_pubkey FROM plugin_install WHERE plugin_id = ?", KEY
    )
    assert code != 0, f"a second distribution declaring {KEY!r} is installed (pin {pin} -> {after})"
    assert after == pin


class _PinningStore:
    """The real store; the admin's pin lands while the update waits on the network."""

    def __init__(self, inner: RepoStore, pin: Any) -> None:
        self._inner, self._pin = inner, pin

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def refresh(self, record: Any, fetcher: Any, *, principal: Any = None) -> Any:
        self._pin()  # a concurrent `POST /plugins/{key}/pin` between the check and the write
        return await self._inner.refresh(record, fetcher, principal=principal)


@pytest.mark.req("EF-SRC-020")
def test_pin_during_update_is_overridden(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=_wheel(tmp_path, "1.0.0"), plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=_wheel(tmp_path, "2.0.0"), plugin_version="2.0.0")

    engine = create_db_engine(settings.database_path)
    layout = guard.Layout.of(settings.data_dir)
    try:
        store = _PinningStore(
            RepoStore(
                engine, now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC)
            ),
            lambda: lifecycle.pin(engine, layout, KEY, DIST),
        )
        with pytest.raises(lifecycle.PluginLifecycleError):
            asyncio.run(lifecycle.update(engine, store, layout, KEY, DIST, newer))  # type: ignore[arg-type]
    finally:
        engine.dispose()
    row = _install_row(settings)
    assert not (row[1] and _state_version(settings) == "2.0.0"), f"pinned and moved anyway: {row}"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.mark.req("EF-SRC-020", "ENF-OBS-003")
def test_http_rollback_refused_by_the_installer_is_a_problem_not_a_500(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    first, second = _wheel(tmp_path, "1.0.0"), _wheel(tmp_path, "2.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _n: newer)
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) == 0

    real = lifecycle.install_wheel

    def install(layout: Any, wheel: Path, sha256: str, **kwargs: Any) -> Any:
        if sha256 == "sha256:" + first.sha256:
            raise guard.PluginInstallError("install_failed", "the installer refused")
        return real(layout, wheel, sha256, **kwargs)

    monkeypatch.setattr("shelfhold.plugin_host.lifecycle.install_wheel", install)
    api.login()
    try:
        answer = api.post(f"/api/v1/plugins/{KEY}/rollback")
        status = answer.status_code
    except guard.PluginInstallError:
        status = 500
    assert status != 500, "the guard's refusal escapes the route (500, no refusal line)"
    assert ("plugin.rollback_refused", f"{KEY}:install_failed") in db_rows(
        settings, "SELECT action, target FROM audit"
    )
