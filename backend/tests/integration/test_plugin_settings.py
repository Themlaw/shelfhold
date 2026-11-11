"""M2 L07 (EF-SRC-013): per-source settings, secrets and cookies sealed at rest.

A source declares its settings in `settings_schema` (a small JSON Schema). Plain values live in
`source_config.settings`; a secret field (`writeOnly` or `format: password`) and the cookie jar
are sealed one by one in `source_secret` with the ENF-SEC-005 envelope, whose associated data is
the source and the field: a sealed value moved to another source or field does not open. Nothing
reads a secret back: the API and the CLI say "set" or "not set" and a short keyed fingerprint.
Proxy, anti-bot solver, TLS fingerprint and headless browser stay reserved.

Test values are generated at run time (`secrets.token_hex`): nothing for a secret scanner.
"""

import secrets
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.logging import known_secret_values
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secret_text import secret_forms
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.errors import DecryptionError
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.ops.backup.restore import restore_archive
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.plugin_host.settings import (
    RESERVED_SETTINGS,
    SettingsSchemaError,
    SettingValueError,
    SourceSettings,
    parse_settings_schema,
    secret_row_id,
)
from shelfhold.sdk.models import API_VERSION
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService
from tests.auth_env import CSRF_HEADER, auth_env, db_dump
from tests.backup_env import backup_bench, read_members
from tests.redaction_support import forget_secret_values

pytestmark = pytest.mark.req("EF-SRC-013")

KEY = "acme"
OTHER = "bravo"
ADMIN = Actor("session:admin-test")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lang": {"type": "string", "enum": ["fr", "en"], "default": "fr"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
        "api_key": {"type": "string", "writeOnly": True},
        "password": {"type": "string", "format": "password"},
    },
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    forget_secret_values()
    yield
    forget_secret_values()


@pytest.fixture
def bench(settings: Settings) -> Iterator[tuple[sa.Engine, SecretVault]]:
    migrate(settings)
    vault = SecretVault(boot_check(settings, announce=False))
    engine = create_db_engine(settings.database_path)
    yield engine, vault
    engine.dispose()


def _forms(*values: str) -> set[str]:
    return set().union(*(secret_forms(v) for v in values))


def _dump(engine: sa.Engine) -> str:
    raw = engine.raw_connection()
    try:
        return "\n".join(raw.driver_connection.iterdump())  # type: ignore[attr-defined]
    finally:
        raw.close()


def test_source_settings_secrets_encrypted(bench: tuple[sa.Engine, SecretVault]) -> None:
    """Given a configured source, when the database is dumped, then no secret shows in clear."""
    engine, vault = bench
    schema = parse_settings_schema(SCHEMA)
    store = SourceSettings(engine, vault)
    key_value, password, cookie = (secrets.token_hex(16) for _ in range(3))
    store.set_values(KEY, schema, {"lang": "en", "page_size": 20}, actor=ADMIN)
    store.set_secret(KEY, schema, "api_key", key_value, actor=ADMIN)
    store.set_secret(KEY, schema, "password", password, actor=ADMIN)
    store.set_cookies(KEY, {"sid": cookie}, actor=ADMIN)

    dump = _dump(engine)
    for form in _forms(key_value, password, cookie):
        assert form not in dump
    assert dump.count('INSERT INTO "source_secret"') == 3
    loaded = store.load(KEY)
    assert loaded.values == {"api_key": key_value, "password": password}
    assert loaded.cookies == {"sid": cookie}
    assert key_value not in repr(loaded) and cookie not in repr(loaded)
    view = store.view(KEY, schema)
    assert view.values == {"lang": "en", "page_size": 20}
    assert view.secrets["api_key"].set and view.secrets["password"].set and view.cookies.set
    assert len(view.secrets["api_key"].fingerprint or "") == 8
    assert key_value not in repr(view)


def test_a_sealed_setting_moved_to_another_source_or_field_does_not_open(
    bench: tuple[sa.Engine, SecretVault],
) -> None:
    engine, vault = bench
    schema = parse_settings_schema(SCHEMA)
    store = SourceSettings(engine, vault)
    store.set_secret(KEY, schema, "api_key", secrets.token_hex(16), actor=ADMIN)
    store.set_secret(OTHER, schema, "api_key", secrets.token_hex(16), actor=ADMIN)
    store.set_secret(KEY, schema, "password", secrets.token_hex(16), actor=ADMIN)
    moved = sa.text(
        "UPDATE source_secret SET (secret_kid, secret_v, secret_nonce, secret_ct) = "
        "(SELECT s.secret_kid, s.secret_v, s.secret_nonce, s.secret_ct FROM source_secret s "
        "WHERE s.id = :source) WHERE id = :target"
    )
    for target, source in (
        (secret_row_id(OTHER, "secret", "api_key"), secret_row_id(KEY, "secret", "api_key")),
        (secret_row_id(KEY, "secret", "password"), secret_row_id(KEY, "secret", "api_key")),
    ):
        with engine.begin() as connection:
            connection.execute(moved, {"source": source, "target": target})
    with pytest.raises(DecryptionError):
        store.load(OTHER)
    with pytest.raises(DecryptionError):
        store.load(KEY)
    with engine.connect() as connection:  # the row id is the associated data: source and field
        ids = connection.execute(sa.text("SELECT id, plugin_id, kind, field FROM source_secret"))
        for row_id, plugin_id, kind, field in ids:
            assert row_id == secret_row_id(plugin_id, kind, field)


def test_every_form_of_a_loaded_secret_is_registered_at_once(
    bench: tuple[sa.Engine, SecretVault],
) -> None:
    engine, vault = bench
    schema = parse_settings_schema(SCHEMA)
    store = SourceSettings(engine, vault)
    key_value, cookie = secrets.token_hex(16), secrets.token_hex(16)
    store.set_secret(KEY, schema, "api_key", key_value, actor=ADMIN)
    store.set_cookies(KEY, {"sid": cookie, "pref": secrets.token_hex(12)}, actor=ADMIN)
    forget_secret_values()  # another process: nothing is known until it loads
    assert not _forms(key_value, cookie) & known_secret_values()
    loaded = store.load(KEY)
    header = "; ".join(f"{n}={v}" for n, v in sorted(loaded.cookies.items()))
    assert _forms(key_value, cookie, f"sid={cookie}", header) <= known_secret_values()


def test_settings_schema_is_validated() -> None:
    assert parse_settings_schema({}).fields == ()
    for bad in (
        {"type": "array"},
        {"type": "object", "properties": {"x": {"type": "object"}}},
        {"type": "object", "properties": {"Bad-Name": {"type": "string"}}},
        {
            "type": "object",
            "properties": {"x": {"type": "string", "writeOnly": True, "default": "a"}},
        },
        {"type": "object", "properties": {"x": {"type": "integer", "writeOnly": True}}},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": ".*"}}},
        {"type": "object", "properties": {"cookies": {"type": "string"}}},
        {"type": "object", "properties": {}, "additionalProperties": True},
    ):
        with pytest.raises(SettingsSchemaError):
            parse_settings_schema(bad)
    schema = parse_settings_schema(SCHEMA)
    assert [f.name for f in schema.fields if f.secret] == ["api_key", "password"]
    assert schema.validate_values({"lang": "en"}) == {"lang": "en"}
    for values in ({"lang": "de"}, {"page_size": 0}, {"page_size": "3"}, {"page_size": True},
                   {"unknown": 1}, {"api_key": "x"}):  # fmt: skip
        with pytest.raises(SettingValueError) as caught:
            schema.validate_values(values)
        for value in values.values():
            if isinstance(value, str) and len(value) > 1:
                assert value not in str(caught.value)  # the field is named, never the value


@pytest.mark.parametrize("name", sorted(RESERVED_SETTINGS))
def test_network_evasion_settings_stay_reserved(name: str) -> None:
    """Proxy, solver, TLS fingerprint and headless browser are reserved for the first native
    source that needs them, behind the same SSRF guard: no plugin declares one in M2."""
    with pytest.raises(SettingsSchemaError):
        parse_settings_schema({"type": "object", "properties": {name: {"type": "string"}}})
    raw = FakeRangedConnectedService().manifest.model_dump(mode="json")
    reserved = {"type": "object", "properties": {name: {"type": "string"}}}
    raw = {**raw, "key": KEY, "api": API_VERSION, "settings_schema": reserved}
    registry = PluginRegistry()
    (record,) = registry.load_all(
        [PluginCandidate(KEY, PluginOrigin.FIRST_PARTY, lambda: SimpleNamespace(manifest=raw))]
    )
    assert record.status.value == "incompatible" and "settings_schema" in (record.reason or "")


def test_secrets_stay_sealed_in_a_backup_and_open_after_restore(tmp_path: Path) -> None:
    key_value, cookie = secrets.token_hex(16), secrets.token_hex(16)
    schema = parse_settings_schema(SCHEMA)
    with backup_bench(tmp_path) as bench:
        vault = SecretVault(boot_check(bench.settings, create=False, announce=False))
        store = SourceSettings(bench.engine, vault)
        store.set_secret(KEY, schema, "api_key", key_value, actor=ADMIN)
        store.set_cookies(KEY, {"sid": cookie}, actor=ADMIN)
        archive = bench.backups / bench.service.run().name
        members = read_members(archive)
        assert "shelfhold.db" in members
        for name, data in members.items():
            text = data.decode("latin-1")
            assert not [f for f in _forms(key_value, cookie) if f in text], name
        target = tmp_path / "restored"
        restore_archive(archive, target, bench.passphrase, live_data_dir=bench.settings.data_dir)
    restored = Settings(data_dir=target, store_dir=tmp_path / "store2")
    forget_secret_values()
    engine = create_db_engine(target / "shelfhold.db")
    try:
        again = SourceSettings(engine, SecretVault(boot_check(restored, create=False)))
        loaded = again.load(KEY)
    finally:
        engine.dispose()
    assert loaded.values == {"api_key": key_value} and loaded.cookies == {"sid": cookie}


def _registry_with_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = FakeRangedConnectedService().manifest.model_dump(mode="json")
    raw = {**raw, "key": KEY, "api": API_VERSION, "settings_schema": SCHEMA}

    def start(*_a: object, **_k: object) -> tuple[PluginRegistry, list[Any]]:
        registry = PluginRegistry()
        records = registry.load_all(
            [PluginCandidate(KEY, PluginOrigin.FIRST_PARTY, lambda: SimpleNamespace(manifest=raw))]
        )
        return registry, records

    monkeypatch.setattr("shelfhold.main.start_plugins", start)


def test_api_never_reads_a_secret_back_and_setting_one_is_reserved(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    key_value, cookie = secrets.token_hex(16), secrets.token_hex(16)
    _registry_with_schema(monkeypatch)
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200
        headers = {CSRF_HEADER: api.csrf()}
        base = f"/api/v1/sources/{KEY}"
        empty = api.client.get(f"{base}/settings")
        assert empty.status_code == 200, empty.text
        assert empty.json()["secrets"]["api_key"] == {"set": False, "fingerprint": None}
        put = api.client.put(f"{base}/secrets/api_key", json={"value": key_value}, headers=headers)
        assert put.status_code == 200, put.text
        assert put.json()["set"] is True and key_value not in put.text
        jar = api.client.put(f"{base}/cookies", json={"cookies": {"sid": cookie}}, headers=headers)
        assert jar.status_code == 200 and cookie not in jar.text
        plain = api.client.patch(
            f"{base}/settings", json={"values": {"lang": "en"}}, headers=headers
        )
        assert plain.status_code == 200 and plain.json()["values"]["lang"] == "en"
        # a secret given where a plain value goes is refused, and never echoed
        wrong = api.client.patch(
            f"{base}/settings", json={"values": {"api_key": key_value}}, headers=headers
        )
        assert wrong.status_code == 422 and key_value not in wrong.text
        bad = api.client.patch(
            f"{base}/settings", json={"values": {"page_size": key_value}}, headers=headers
        )
        assert bad.status_code == 422 and key_value not in bad.text
        read = api.client.get(f"{base}/settings")
        assert read.json()["secrets"]["api_key"]["set"] is True
        assert len(read.json()["secrets"]["api_key"]["fingerprint"]) == 8
        assert read.json()["cookies"]["set"] is True
        assert key_value not in read.text and cookie not in read.text
        api.clock.advance(timedelta(minutes=20))  # the password was typed 20 minutes ago
        stale = api.client.put(
            f"{base}/secrets/api_key", json={"value": secrets.token_hex(16)}, headers=headers
        )
        assert stale.status_code in {401, 403} and stale.json()["code"] == "reauth_required"
        unknown = api.client.get("/api/v1/sources/nobody/settings")
        assert unknown.status_code == 404
    dump = db_dump(settings)
    assert key_value not in dump and cookie not in dump
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            audit = [
                (r[0], r[1])
                for r in connection.execute(
                    sa.text(
                        "SELECT action, target FROM audit WHERE action LIKE 'source.%' "
                        "ORDER BY at, id"
                    )
                )
            ]
    finally:
        engine.dispose()
    assert ("source.secret_set", f"{KEY}:api_key") in audit
    assert ("source.cookies_set", f"{KEY}:cookies") in audit
    assert ("source.settings_changed", f"{KEY}:lang") in audit
    assert all(key_value not in (t or "") for _a, t in audit)
