"""M6 closure re-check, lot A: what the fixes of the second reading promise.

A hold never lands on another hold (R1), a revocation that deactivates a running plugin says a
restart is required (R2), and a wheel is opened only once its sha256 is verified (R5).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.index import hold_previous_install
from shelfhold.plugin_host.public import refuse_taken_keys
from shelfhold.plugin_host.settings import held_plugin_data, parse_settings_schema
from tests.integration.test_l08_plugin_signer_pin import SCHEMA
from tests.integration.test_l09a_plugin_lifecycle import KEY
from tests.integration.test_m6_closure_review_lifecycle import SECRET, _store
from tests.integration.test_m6_closure_review_revocation import (
    _installed_and_enabled,
    _refresh,
    _revoking,
)
from tests.plugin_index_env import FakeFetcher, make_key

pytestmark = pytest.mark.req("EF-SRC-017")

SIGNER = make_key()
FROM_A = ("https://a.example.org/index.json", SIGNER.public_b64)


def _vault(settings: Settings) -> SecretVault:
    return SecretVault(boot_check(settings, announce=False))


def _configured(settings: Settings, lang: str) -> None:
    engine, store = _store(settings)
    try:
        schema = parse_settings_schema(SCHEMA)
        store.set_values(KEY, schema, {"lang": lang}, actor=Actor("cli:uid1000"))
        store.set_secret(KEY, schema, "api_key", SECRET, actor=Actor("cli:uid1000"))
    finally:
        engine.dispose()


def test_a_second_hold_goes_beside_the_first_and_leaves_the_key_bare(settings: Settings) -> None:
    migrate(settings)
    _configured(settings, "en")
    engine, _ = _store(settings)
    try:
        first = hold_previous_install(engine, _vault(settings), KEY, FROM_A, (None, None))
    finally:
        engine.dispose()
    _configured(settings, "fr")
    engine, store = _store(settings)
    try:
        second = hold_previous_install(engine, _vault(settings), KEY, FROM_A, (None, None))
        assert (
            first is not None and second is not None and second != first
        )  # none lost, none merged
        assert held_plugin_data(engine, KEY) == sorted([first, second])
        assert store.load(KEY).values.get("lang") != "fr"  # the key itself is left bare
    finally:
        engine.dispose()


def test_the_audit_of_a_hold_names_the_identity_the_data_waits_under(settings: Settings) -> None:
    migrate(settings)
    _configured(settings, "en")
    engine, _ = _store(settings)
    told: list[str] = []
    try:
        first = hold_previous_install(
            engine,
            _vault(settings),
            KEY,
            FROM_A,
            (None, None),
            audit=lambda held, _c: told.append(held),
        )
        _configured(settings, "fr")
        second = hold_previous_install(
            engine,
            _vault(settings),
            KEY,
            FROM_A,
            (None, None),
            audit=lambda held, _c: told.append(held),
        )
    finally:
        engine.dispose()
    assert told == [first, second]


def test_a_refresh_that_revokes_a_running_plugin_says_a_restart_is_required(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    signer, wheel = _installed_and_enabled(monkeypatch, settings, tmp_path)
    capsys.readouterr()
    newer = FakeFetcher()
    _revoking(newer, signer, wheel, version=2, plugin_keys=[KEY])
    assert _refresh(monkeypatch, settings, newer) == 0
    out = capsys.readouterr().out
    assert KEY in out and "redémarr" in out.lower()  # the default locale is French


def test_a_wheel_is_not_opened_before_its_sha256_is_verified(tmp_path: Path) -> None:
    layout = guard.Layout.of(tmp_path / "data")
    wheel = tmp_path / "x-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"not an archive")
    with pytest.raises(guard.PluginInstallError) as refused:
        refuse_taken_keys(layout, wheel, "0" * 64)
    assert refused.value.code == "hash_mismatch"  # never `bad_wheel`: the file was not read
