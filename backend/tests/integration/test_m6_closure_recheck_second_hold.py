"""Contre-relecture ciblée M6, lot A (S2) : une seconde mise de côté sous la même identité.

Le signataire A a déjà des données en attente sous ``acme@<empreinte A>`` (une première roue à la
main est passée). L'admin réinstalle depuis l'index de A, reconfigure, retire, puis redonne une
roue à la main : la mise de côté vise de nouveau ``acme@<empreinte A>``, la clé primaire de
``source_config`` est déjà prise, la transaction échoue — mais `run_install` a déjà installé la
roue (la mise de côté vient après `install_wheel`) et ne rattrape que `PluginDataError`. La roue
non signée reste installée et lit les réglages et secrets du signataire A.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.infrastructure.migrate import migrate
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.settings import parse_settings_schema
from tests.auth_env import db_rows, run_cli
from tests.integration.test_l08_plugin_signer_pin import SCHEMA
from tests.integration.test_l09a_plugin_lifecycle import (
    DIST,
    KEY,
    _install,
    _served,
    _wheel,
)
from tests.integration.test_m6_closure_review_lifecycle import SECRET, _add_index, _evil, _store
from tests.plugin_index_env import FakeFetcher, make_key

pytestmark = pytest.mark.req("EF-SRC-017")


def _configure(settings: Settings, lang: str) -> None:
    engine, store = _store(settings)
    try:
        schema = parse_settings_schema(SCHEMA)
        store.set_values(KEY, schema, {"lang": lang}, actor=Actor("cli:uid1000"))
        store.set_secret(KEY, schema, "api_key", SECRET, actor=Actor("cli:uid1000"))
    finally:
        engine.dispose()


def test_a_second_hold_under_the_same_identity_leaves_the_hand_wheel_with_a_secrets(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=_wheel(tmp_path, "1.0.0"), plugin_version="1.0.0")
    evil = _evil(tmp_path)
    hand = ("plugin", "install", str(evil.path), "--sha256", evil.sha256)

    # 1. A signé, configuré ; retiré ; roue à la main : les données de A partent sous acme@A
    assert _install(monkeypatch, settings, fetcher) == 0
    _configure(settings, "en")
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    assert run_cli(monkeypatch, settings, *hand) == 0
    # 2. retour à A depuis son index, reconfiguré
    assert run_cli(monkeypatch, settings, "plugin", "remove", "evil-plug") == 0
    assert _install(monkeypatch, settings, fetcher) == 0
    _configure(settings, "fr")
    # 3. de nouveau la roue à la main
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    try:
        code: object = run_cli(monkeypatch, settings, *hand)
    except Exception as exc:  # noqa: BLE001 - the CLI must refuse, not crash after the install
        code = repr(exc)

    installed = "evil-plug" in guard.read_state(guard.Layout.of(settings.data_dir)).installed
    engine, store = _store(settings)
    try:
        inherited = store.load(KEY).values.get("api_key") == SECRET
    finally:
        engine.dispose()
    rows = db_rows(settings, "SELECT plugin_id FROM source_config")
    assert not (installed and inherited), (
        f"the hand wheel is installed and reads signer A's secret (cli: {code}; config rows: {rows})"
    )
