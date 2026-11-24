"""M6 L09a, relecture adverse: what the lifecycle must refuse and does not (red PoC).

- a reinstall from **another signer** reattaches the bindings the removal left orphaned;
- a wheel given **by hand** reattaches the bindings an index-signed install left orphaned;
- an ``update`` installs code from a signer the install was never pinned to;
- a ``rollback`` puts back a version the index **revoked** (its wheel or its key).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from tests.auth_env import db_rows, run_cli
from tests.integration.test_l09a_plugin_lifecycle import (
    DIST,
    KEY,
    URL,
    WHEEL_BASE,
    _binding,
    _expires,
    _install,
    _seed_library,
    _served,
    _state_version,
    _wheel,
)
from tests.plugin_index_env import (
    FakeFetcher,
    index_document,
    make_key,
    plugin_entry,
    sign_index,
    trusted_comment,
)

pytestmark = pytest.mark.req("EF-SRC-020")

OTHER_URL = "https://other.example.org/index.json"


def _add_index(monkeypatch: pytest.MonkeyPatch, settings: Settings, url: str, pubkey: str) -> None:
    assert run_cli(monkeypatch, settings, "plugin", "index", "add", url, "--pubkey", pubkey) == 0


def _use(monkeypatch: pytest.MonkeyPatch, fetcher: FakeFetcher) -> None:
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: fetcher)


@pytest.mark.req("EF-SRC-020", "D15")
def test_reinstall_from_another_signer_does_not_reattach_orphans(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    first, other = make_key(), make_key()
    _add_index(monkeypatch, settings, URL, first.public_b64)
    _add_index(monkeypatch, settings, OTHER_URL, other.public_b64)
    wheel = _wheel(tmp_path, "1.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, first, version=1, wheel=wheel, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    ids = _seed_library(settings)
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    assert _binding(settings, ids["binding"]) == (0, "orphaned")

    # the same key, published by another signer: a distinct install, it inherits nothing
    elsewhere = FakeFetcher()
    _served(elsewhere, other, version=1, wheel=wheel, plugin_version="1.0.0", url=OTHER_URL)
    assert _install(monkeypatch, settings, elsewhere, url=OTHER_URL) == 0
    assert _binding(settings, ids["binding"]) == (0, "orphaned")


@pytest.mark.req("EF-SRC-020", "D15")
def test_hand_wheel_does_not_reattach_orphans_of_an_index_install(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, URL, signer.public_b64)
    wheel = _wheel(tmp_path, "1.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=wheel, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    ids = _seed_library(settings)
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    assert _binding(settings, ids["binding"]) == (0, "orphaned")

    # a wheel the admin gives by hand is not the signer's word
    assert (
        run_cli(
            monkeypatch, settings, "plugin", "install", str(wheel.path), "--sha256", wheel.sha256
        )
        == 0
    )
    assert _binding(settings, ids["binding"]) == (0, "orphaned")


@pytest.mark.req("EF-SRC-020", "D15")
def test_update_refuses_a_signer_the_install_is_not_pinned_to(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    signer, intruder = make_key(), make_key()
    _add_index(monkeypatch, settings, URL, signer.public_b64)
    first, second = _wheel(tmp_path, "1.0.0"), _wheel(tmp_path, "2.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0

    # the same address, pinned again to another key: L08 calls that a distinct install
    assert run_cli(monkeypatch, settings, "plugin", "index", "remove", URL) == 0
    _add_index(monkeypatch, settings, URL, intruder.public_b64)
    newer = FakeFetcher()
    _served(newer, intruder, version=1, wheel=second, plugin_version="2.0.0")
    _use(monkeypatch, newer)
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) != 0
    assert _state_version(settings) == "1.0.0"


@pytest.mark.req("EF-SRC-020", "D15")
@pytest.mark.parametrize("revoked", ["wheel", "key"])
def test_rollback_refuses_a_revoked_previous_version(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, revoked: str
) -> None:
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, URL, signer.public_b64)
    first, second = _wheel(tmp_path, "1.0.0"), _wheel(tmp_path, "2.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    _use(monkeypatch, newer)
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) == 0
    assert _state_version(settings) == "2.0.0"

    # the index revokes the previous version (its wheel, or the whole key) and is refreshed
    revoking = FakeFetcher()
    expires = _expires()
    document = index_document(
        version=3,
        expires=expires,
        plugins=[
            plugin_entry(
                key=KEY, version="2.0.0", sha256="sha256:" + second.sha256,
                wheel_url=WHEEL_BASE + second.path.name,
            )
        ],
        wheel_sha256=["sha256:" + first.sha256] if revoked == "wheel" else None,
        plugin_keys=[KEY] if revoked == "key" else None,
    )  # fmt: skip
    revoking.serving(
        URL, document, sign_index(document, signer, comment=trusted_comment(3, expires))
    )
    _use(monkeypatch, revoking)
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0

    assert run_cli(monkeypatch, settings, "plugin", "rollback", KEY) != 0
    assert _state_version(settings) == "2.0.0"
    assert ("plugin.rollback", KEY) not in db_rows(settings, "SELECT action, target FROM audit")
