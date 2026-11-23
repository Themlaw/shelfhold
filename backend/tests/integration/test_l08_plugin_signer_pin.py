"""M6 L08 (EF-SRC-017 passe 8, GAP-8-13): an install from an index, its key pinned to its signer.

A plugin key is pinned, at install time, to the triple ``(key, index, signer)`` the admin chose:
the repository's URL and the public key its index is signed with, kept in ``plugin_install``. A
second index that publishes the same key signed by **another signer** is never an update: the
install is refused and a distinct install is proposed. The distinct install starts empty — the
state, the settings and the secrets of the previous install are held under its signer's
fingerprint, and move only on the admin's confirmed word (a reserved action). And whatever the
signature, the trust card of the module stays « Inconnu » until something is attested: a signed
index vouches for a file's bytes, never for a publisher's conduct.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.identity.public import RESERVED_ACTIONS
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host.registry import PluginOrigin, PluginRecord, PluginStatus
from shelfhold.plugin_host.settings import SourceSettings, parse_settings_schema
from shelfhold.plugin_host.trust_card import TrustLevel, trust_level
from tests.auth_env import db_rows, run_cli
from tests.plugin_guard_env import build_wheel
from tests.plugin_index_env import (
    WHEEL_URL,
    FakeFetcher,
    IndexKey,
    index_document,
    make_key,
    plugin_entry,
    sign_index,
    trusted_comment,
)

pytestmark = pytest.mark.req("EF-SRC-017")

URL = "https://index.example.org/index.json"
OTHER_URL = "https://other.example.org/index.json"
DIST = "acme-plug"
KEY = "acme"
MODULE = "acme_plug"
SCHEMA = {
    "type": "object",
    "properties": {
        "lang": {"type": "string", "enum": ["fr", "en"], "default": "fr"},
        "api_key": {"type": "string", "writeOnly": True},
    },
}


def _expires(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wheel(tmp_path: Path) -> Any:
    return build_wheel(
        tmp_path,
        DIST,
        {f"{MODULE}/__init__.py": "x = 1\n"},
        entry=(KEY, f"{MODULE}:plugin"),
    )


def _served(
    fetcher: FakeFetcher,
    key: IndexKey,
    *,
    version: int,
    plugins: list[dict[str, Any]] | None = None,
    **over: Any,
) -> None:
    expires = _expires()
    document = index_document(
        version=version,
        expires=expires,
        plugins=plugins
        if plugins is not None
        else [plugin_entry(key=KEY, sha256="sha256:" + _wheel_sha(fetcher))],
        **over,
    )
    fetcher.serving(
        URL, document, sign_index(document, key, comment=trusted_comment(version, expires))
    )


def _wheel_sha(fetcher: FakeFetcher) -> str:
    """The sha256 of the wheel the fetcher serves, as the index entry vouches for it."""
    data = fetcher.wheels.get(WHEEL_URL)
    assert data is not None
    return hashlib.sha256(data).hexdigest()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    fetcher: FakeFetcher,
    key: str = KEY,
    *,
    url: str = URL,
) -> int:
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: fetcher)
    return run_cli(monkeypatch, settings, "plugin", "install", key, "--index", url)


def _pin(settings: Settings, key: str = KEY) -> tuple[Any, ...]:
    return db_rows(
        settings, "SELECT index_url, signer_pubkey FROM plugin_install WHERE plugin_id = ?", key
    )


def _installed(settings: Settings) -> list[str]:
    state = settings.data_dir / "plugins" / "state.json"
    if not state.exists():
        return []
    return sorted(json.loads(state.read_text(encoding="utf-8")).get("installed", {}))


def _plugin_rows(settings: Settings, table: str) -> list[tuple[Any, ...]]:
    return db_rows(settings, f"SELECT plugin_id FROM {table} ORDER BY plugin_id")  # noqa: S608


# -- the pin: (key, index, signer) is kept, and the wheel comes through the Fetcher ------------


def test_plugin_key_pinned_to_signer(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    key = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", key.public_b64)
        == 0
    )
    wheel = _wheel(tmp_path)

    fetcher = FakeFetcher()
    fetcher.wheels[WHEEL_URL] = wheel.path.read_bytes()
    _served(fetcher, key, version=1)
    assert _install(monkeypatch, settings, fetcher) == 0

    # the triple is pinned: the key, the index it came from, the signer of that index
    assert _pin(settings) == [(URL, key.public_b64)]
    assert _installed(settings) == [DIST]
    assert WHEEL_URL in fetcher.calls  # the wheel went through the Fetcher, like the index

    # an install over an installed plugin is refused, and the pin does not move
    assert _install(monkeypatch, settings, fetcher) != 0
    assert _pin(settings) == [(URL, key.public_b64)]
    assert db_rows(
        settings, "SELECT action, target FROM audit WHERE action = 'plugin.install_refused'"
    ) == [("plugin.install_refused", f"{KEY}:conflict")]


# -- a new signer is a new install, never an update ---------------------------------------------


def test_signer_change_proposes_distinct_install(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(settings)
    first, second = make_key(), make_key()
    wheel = _wheel(tmp_path)
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", first.public_b64)
        == 0
    )
    assert (
        run_cli(
            monkeypatch,
            settings,
            "plugin",
            "index",
            "add",
            OTHER_URL,
            "--pubkey",
            second.public_b64,
        )
        == 0
    )

    fetcher = FakeFetcher()
    fetcher.wheels[WHEEL_URL] = wheel.path.read_bytes()
    _served(fetcher, first, version=1)
    assert _install(monkeypatch, settings, fetcher) == 0

    # a second index publishes the same key, signed by another signer
    other = FakeFetcher()
    other.wheels[WHEEL_URL] = wheel.path.read_bytes()
    expires = _expires()
    document = index_document(
        version=1,
        expires=expires,
        plugins=[plugin_entry(key=KEY, sha256="sha256:" + _wheel_sha(other))],
    )
    other.serving(
        OTHER_URL, document, sign_index(document, second, comment=trusted_comment(1, expires))
    )
    assert _install(monkeypatch, settings, other, url=OTHER_URL) != 0
    out = capsys.readouterr()
    assert "distinct" in (out.out + out.err).lower()  # the refusal proposes the distinct install
    assert db_rows(
        settings, "SELECT action, target FROM audit WHERE action = 'plugin.install_refused'"
    ) == [("plugin.install_refused", f"{KEY}:signer_pinned")]
    assert _pin(settings) == [(URL, first.public_b64)]  # nothing moved
    assert _installed(settings) == [DIST]

    # the distinct install: the installed plugin goes first, then the other signer's is installed
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    assert _install(monkeypatch, settings, other, url=OTHER_URL) == 0
    assert _pin(settings) == [(OTHER_URL, second.public_b64)]


# -- state, settings and secrets move only on the admin's confirmed word -------------------------


def test_state_and_secrets_moved_only_after_confirmation(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    boot_check(settings, announce=False)  # the keyring: a secret is seeded, and must move sealed
    first, second = make_key(), make_key()
    wheel = _wheel(tmp_path)
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", first.public_b64)
        == 0
    )
    assert (
        run_cli(
            monkeypatch,
            settings,
            "plugin",
            "index",
            "add",
            OTHER_URL,
            "--pubkey",
            second.public_b64,
        )
        == 0
    )

    engine = create_db_engine(settings.database_path)
    store = SourceSettings(engine, SecretVault(boot_check(settings, announce=False)))
    schema = parse_settings_schema(SCHEMA)
    actor = Actor("cli:uid1000")
    store.set_values(KEY, schema, {"lang": "en"}, actor=actor)
    store.set_secret(KEY, schema, "api_key", "secret-value-7f3a51", actor=actor)
    store.set_cookies(KEY, {"sid": "cookie-4471"}, actor=actor)
    with engine.connect() as connection:  # the plugin's own state, one document per key
        connection.execute(
            sa.insert(acquisition.plugin_state).values(
                plugin_id=KEY, key="shelf", payload={"n": 1}, payload_schema=1
            )
        )
        connection.commit()
    fingerprints = db_rows(
        settings, "SELECT kind, field, fingerprint FROM source_secret WHERE plugin_id = ?", KEY
    )
    assert len(fingerprints) == 2

    fetcher = FakeFetcher()
    fetcher.wheels[WHEEL_URL] = wheel.path.read_bytes()
    _served(fetcher, first, version=1)
    assert _install(monkeypatch, settings, fetcher) == 0
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0

    # the distinct install: nothing of the previous signer's is inherited
    other = FakeFetcher()
    other.wheels[WHEEL_URL] = wheel.path.read_bytes()
    expires = _expires()
    document = index_document(
        version=1,
        expires=expires,
        plugins=[plugin_entry(key=KEY, sha256="sha256:" + _wheel_sha(other))],
    )
    other.serving(
        OTHER_URL, document, sign_index(document, second, comment=trusted_comment(1, expires))
    )
    assert _install(monkeypatch, settings, other, url=OTHER_URL) == 0
    held = f"{KEY}@{first.key_id}"
    assert _plugin_rows(settings, "plugin_state") == [(held,)]
    assert _plugin_rows(settings, "source_config") == [(held,)]
    assert _plugin_rows(settings, "source_secret") == [(held,)]
    assert db_rows(
        settings, "SELECT kind, field, fingerprint FROM source_secret WHERE plugin_id = ?", held
    ) == sorted(fingerprints)  # sealed again, the same values, the same fingerprints
    view = store.view(KEY, schema)  # the new install sees nothing of the previous one
    assert view.values["lang"] == "fr"
    assert not view.secrets["api_key"].set and not view.cookies.set
    assert ("plugin.state_held", held) in db_rows(settings, "SELECT action, target FROM audit")

    # the transfer is a reserved action, and the host CLI is one of its two credentials
    assert "plugin.adopt_state" in RESERVED_ACTIONS
    assert run_cli(monkeypatch, settings, "plugin", "adopt", KEY) == 0
    assert _plugin_rows(settings, "plugin_state") == [(KEY,)]
    assert _plugin_rows(settings, "source_config") == [(KEY,)]
    assert _plugin_rows(settings, "source_secret") == [(KEY,)]
    loaded = store.load(KEY)  # the secret opens again, with the value it always had
    assert loaded.values["api_key"] == "secret-value-7f3a51"
    assert loaded.cookies == {"sid": "cookie-4471"}
    assert store.view(KEY, schema).values["lang"] == "en"
    assert ("plugin.adopt", held) in db_rows(settings, "SELECT action, target FROM audit")

    # once moved, there is nothing left to confirm
    assert run_cli(monkeypatch, settings, "plugin", "adopt", KEY) != 0
    engine.dispose()


# -- a signed index alone never makes a module community -----------------------------------------


def test_signed_index_alone_never_makes_a_module_community(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    key = make_key()
    wheel = _wheel(tmp_path)
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", key.public_b64)
        == 0
    )
    fetcher = FakeFetcher()
    fetcher.wheels[WHEEL_URL] = wheel.path.read_bytes()
    _served(fetcher, key, version=1)
    assert _install(monkeypatch, settings, fetcher) == 0
    assert _pin(settings) == [(URL, key.public_b64)]  # installed from a signed index

    engine = create_db_engine(settings.database_path)
    try:
        record = PluginRecord(
            key=KEY, origin=PluginOrigin.THIRD_PARTY, status=PluginStatus.DISABLED
        )
        with engine.connect() as connection:
            # the signature vouches for the bytes, not for the publisher: still « Inconnu »
            assert trust_level(record, connection) is TrustLevel.UNKNOWN
        assert (
            run_cli(monkeypatch, settings, "plugin", "attest", KEY, "content_trust", "publisher")
            == 0
        )
        with engine.connect() as connection:
            assert trust_level(record, connection) is TrustLevel.COMMUNITY  # the admin's word
            first_party = PluginRecord(
                key="gutenberg", origin=PluginOrigin.FIRST_PARTY, status=PluginStatus.LOADED
            )
            assert trust_level(first_party, connection) is TrustLevel.OFFICIAL
    finally:
        engine.dispose()


# -- the refusals of an install from an index leave nothing on disk ------------------------------


def test_index_install_refusals_leave_no_trace(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    key = make_key()
    wheel = _wheel(tmp_path)
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", key.public_b64)
        == 0
    )

    def _refused(fetcher: FakeFetcher, expected: str) -> None:
        assert _install(monkeypatch, settings, fetcher) != 0
        assert db_rows(
            settings, "SELECT action, target FROM audit WHERE action = 'plugin.install_refused'"
        ) == [("plugin.install_refused", f"{KEY}:{expected}")]
        plugins = settings.data_dir / "plugins"
        assert not plugins.exists() or not any(plugins.iterdir())  # nothing of a refusal stays
        assert _pin(settings) == []

    # a key the index does not list
    fetcher = FakeFetcher()
    fetcher.wheels[WHEEL_URL] = wheel.path.read_bytes()
    _served(fetcher, key, version=1, plugins=[plugin_entry(key="other")])
    _refused(fetcher, "unknown_plugin")

    # a key the index revoked
    fetcher = FakeFetcher()
    fetcher.wheels[WHEEL_URL] = wheel.path.read_bytes()
    _served(fetcher, key, version=2, plugin_keys=[KEY])
    _refused(fetcher, "plugin_revoked")

    # a wheel the index revoked
    fetcher = FakeFetcher()
    fetcher.wheels[WHEEL_URL] = wheel.path.read_bytes()
    _served(fetcher, key, version=3, wheel_sha256=["sha256:" + _wheel_sha(fetcher)])
    _refused(fetcher, "wheel_revoked")

    # a repository nobody pinned
    assert (
        _install(monkeypatch, settings, FakeFetcher(), url="https://nowhere.example.org/index.json")
        != 0
    )
    assert db_rows(
        settings, "SELECT action, target FROM audit WHERE action = 'plugin.install_refused'"
    ) == [("plugin.install_refused", f"{KEY}:not_found")]
