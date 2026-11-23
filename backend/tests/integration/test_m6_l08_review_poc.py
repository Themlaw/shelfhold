"""M6 L08, relecture adverse: PoC of the defects of the install from an index (EF-SRC-017 passe 8).

Each test states the behaviour the lot promises and is red on the head of the PR:

- the key an index entry names is never compared with the key the wheel declares: a second
  signer lists a decoy key whose wheel declares the key of a removed plugin of the first signer,
  and inherits its state, settings and secrets — no hold, no confirmation;
- a rotation of the index key (L07, double signature: the same signer) makes the next install
  from the same index a "new signer": the data is held as if another publisher had taken over;
- a manual install (a wheel the admin gave by hand) over the row of a removed index install
  keeps the stale pin: the row says "index A, signer A" for code nobody signed;
- `plugin adopt` writes its `plugin.adopt` audit row before a refusal (`occupied`): the audit
  says the data moved when nothing did.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host.settings import SourceSettings, parse_settings_schema
from tests.auth_env import db_rows, run_cli
from tests.integration.test_l08_plugin_signer_pin import (
    DIST,
    KEY,
    OTHER_URL,
    SCHEMA,
    URL,
    WHEEL_BASE,
    _expires,
    _install,
    _pin,
    _plugin_rows,
    _served,
    _wheel,
)
from tests.plugin_guard_env import build_wheel
from tests.plugin_index_env import (
    FakeFetcher,
    IndexKey,
    index_document,
    make_key,
    plugin_entry,
    sign_index,
    trusted_comment,
)

pytestmark = pytest.mark.req("EF-SRC-017")

SECRET = "secret-value-7f3a51"  # noqa: S105 - a synthetic value


def _pin_index(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, url: str, key: IndexKey
) -> None:
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", url, "--pubkey", key.public_b64)
        == 0
    )


def _store(settings: Settings) -> tuple[Any, SourceSettings]:
    engine = create_db_engine(settings.database_path)
    return engine, SourceSettings(engine, SecretVault(boot_check(settings, announce=False)))


def _give_data(store: SourceSettings) -> None:
    schema = parse_settings_schema(SCHEMA)
    actor = Actor("cli:uid1000")
    store.set_values(KEY, schema, {"lang": "en"}, actor=actor)
    store.set_secret(KEY, schema, "api_key", SECRET, actor=actor)


def test_poc_decoy_key_inherits_another_signers_secrets(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Index B lists `decoy`, its wheel declares `acme`: the pin and the hold look at `decoy`."""
    migrate(settings)
    first, second = make_key(), make_key()
    _pin_index(monkeypatch, settings, URL, first)
    _pin_index(monkeypatch, settings, OTHER_URL, second)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    wheel = _wheel(tmp_path / "a")

    fetcher = FakeFetcher()
    _served(fetcher, first, version=1, wheel=wheel)
    assert _install(monkeypatch, settings, fetcher) == 0
    engine, store = _store(settings)
    _give_data(store)  # the admin configures the plugin signer A published
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0

    evil = build_wheel(
        tmp_path / "b", "evil-plug", {"evil_plug/__init__.py": "x = 1\n"},
        entry=(KEY, "evil_plug:plugin"),
    )  # fmt: skip
    other = FakeFetcher()
    wheel_url = WHEEL_BASE + evil.path.name
    other.wheels[wheel_url] = evil.path.read_bytes()
    expires = _expires()
    document = index_document(
        version=1,
        expires=expires,
        plugins=[plugin_entry(key="decoy", sha256="sha256:" + evil.sha256, wheel_url=wheel_url)],
    )
    other.serving(
        OTHER_URL, document, sign_index(document, second, comment=trusted_comment(1, expires))
    )
    code = _install(monkeypatch, settings, other, "decoy", url=OTHER_URL)

    # signer B's code now answers to `acme`: it must not own signer A's secrets unconfirmed
    leaked = store.load(KEY).values.get("api_key") == SECRET and code == 0
    engine.dispose()
    assert not leaked, "signer B's wheel reads the secret signer A's install was given"
    assert code != 0, "a wheel that declares another key than its index entry is installed"
    assert _pin(settings) == [(URL, first.public_b64)]


def test_poc_rotated_index_key_is_still_the_same_signer(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """L07 moves the pin by double signature: the signer is the same, its data stays its own."""
    migrate(settings)
    old, new = make_key(), make_key()
    _pin_index(monkeypatch, settings, URL, old)
    wheel = _wheel(tmp_path)
    fetcher = FakeFetcher()
    _served(fetcher, old, version=1, wheel=wheel)
    assert _install(monkeypatch, settings, fetcher) == 0
    engine, store = _store(settings)
    _give_data(store)
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0

    rotating = FakeFetcher()  # the same document signed by the old key and the next one
    expires = _expires()
    document = index_document(version=2, expires=expires, next_pubkey=new.public_b64)
    rotating.serving(URL, document, sign_index(document, old, comment=trusted_comment(2, expires)))
    rotating.documents[URL + ".next.minisig"] = sign_index(
        document, new, comment=trusted_comment(2, expires)
    )
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _n: rotating)
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0
    assert db_rows(settings, "SELECT pubkey FROM plugin_repo") == [(new.public_b64,)]

    after = FakeFetcher()
    _served(after, new, version=3, wheel=wheel)
    assert _install(monkeypatch, settings, after) == 0
    try:
        assert _plugin_rows(settings, "source_config") == [(KEY,)]  # not held: same signer
        assert store.load(KEY).values["api_key"] == SECRET
    finally:
        engine.dispose()


def test_poc_manual_install_does_not_keep_a_stale_index_pin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """A wheel the admin gave by hand is pinned by the admin's act: the row must say so."""
    migrate(settings)
    key = make_key()
    _pin_index(monkeypatch, settings, URL, key)
    wheel = _wheel(tmp_path)
    fetcher = FakeFetcher()
    _served(fetcher, key, version=1, wheel=wheel)
    assert _install(monkeypatch, settings, fetcher) == 0
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    assert (
        run_cli(
            monkeypatch, settings, "plugin", "install", str(wheel.path), "--sha256", wheel.sha256
        )
        == 0
    )
    assert _pin(settings) == [(None, None)]  # not "installed from index A, signed by A"


def test_poc_refused_adopt_is_not_audited_as_done(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    first, second = make_key(), make_key()
    _pin_index(monkeypatch, settings, URL, first)
    _pin_index(monkeypatch, settings, OTHER_URL, second)
    wheel = _wheel(tmp_path)
    fetcher = FakeFetcher()
    _served(fetcher, first, version=1, wheel=wheel)
    assert _install(monkeypatch, settings, fetcher) == 0
    engine, store = _store(settings)
    _give_data(store)
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    other = FakeFetcher()
    _served(other, second, version=1, wheel=wheel, url=OTHER_URL)
    assert _install(monkeypatch, settings, other, url=OTHER_URL) == 0
    held = f"{KEY}@{first.key_id}"
    schema = parse_settings_schema(SCHEMA)
    store.set_values(KEY, schema, {"lang": "fr"}, actor=Actor("cli:uid1000"))  # its own row
    engine.dispose()

    assert run_cli(monkeypatch, settings, "plugin", "adopt", KEY) != 0  # occupied
    assert _plugin_rows(settings, "source_config") == [(KEY,), (held,)]  # nothing moved
    assert ("plugin.adopt", held) not in db_rows(settings, "SELECT action, target FROM audit")
