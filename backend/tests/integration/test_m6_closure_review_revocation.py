"""M6 closure review: a revocation reaches the installs it binds, and only those.

An index that revokes a key or a wheel disables the installation of that key, which was pinned
to the very (repository, signer) that revoked it: it loses its activation at the refresh, a line
says so, and every start-up judges it again. Another repository, or another signer, never
revokes what this one installed (ADR-0056, D14).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host import lifecycle
from tests.auth_env import db_rows, run_cli
from tests.integration.test_l09a_plugin_lifecycle import (
    DIST,
    KEY,
    URL,
    WHEEL_BASE,
    _expires,
    _install,
    _served,
    _wheel,
)
from tests.plugin_index_env import (
    FakeFetcher,
    index_document,
    make_key,
    sign_index,
    trusted_comment,
)

pytestmark = pytest.mark.req("EF-SRC-017")

OTHER_URL = "https://other.example.org/index.json"


def _revoking(
    fetcher: FakeFetcher,
    signer: Any,
    wheel: Any,
    *,
    version: int,
    url: str = URL,
    plugin_keys: list[str] | None = None,
    wheel_sha256: list[str] | None = None,
) -> None:
    """Serve a signed index that revokes (and lists no plugin)."""
    expires = _expires()
    document = index_document(
        version=version, expires=expires, plugin_keys=plugin_keys, wheel_sha256=wheel_sha256
    )
    fetcher.wheels[WHEEL_BASE + wheel.path.name] = wheel.path.read_bytes()
    fetcher.serving(
        url, document, sign_index(document, signer, comment=trusted_comment(version, expires))
    )


def _refresh(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, fetcher: FakeFetcher, url: str = URL
) -> int:
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _n: fetcher)
    return run_cli(monkeypatch, settings, "plugin", "index", "refresh", url)


def _installed_and_enabled(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> tuple[Any, Any]:
    migrate(settings)
    signer = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", signer.public_b64)
        == 0
    )
    wheel = _wheel(tmp_path, "1.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=wheel, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    assert run_cli(monkeypatch, settings, "plugin", "enable", DIST) == 0
    layout = guard.Layout.of(settings.data_dir)
    assert DIST in guard.read_state(layout).enabled
    return signer, wheel


def _granted(settings: Settings) -> bool:
    layout = guard.Layout.of(settings.data_dir)
    engine = create_db_engine(settings.database_path)
    try:
        revoked = lifecycle.revoked_dists(engine, layout)
    finally:
        engine.dispose()
    (verdict,) = guard.verdicts(layout, dict(os.environ), revoked=revoked)
    return verdict.granted


def test_a_revoked_key_disables_the_installed_plugin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    signer, wheel = _installed_and_enabled(monkeypatch, settings, tmp_path)
    assert _granted(settings)
    newer = FakeFetcher()
    _revoking(newer, signer, wheel, version=2, plugin_keys=[KEY])
    assert _refresh(monkeypatch, settings, newer) == 0

    state = guard.read_state(guard.Layout.of(settings.data_dir))
    assert DIST not in state.enabled and DIST not in state.confirmed  # lost its activation
    assert ("plugin.revoked", f"{KEY}:plugin_revoked") in db_rows(
        settings, "SELECT action, target FROM audit"
    )
    assert not _granted(settings)

    # the start-up judges it again: an activation given back is read against the revocation
    assert run_cli(monkeypatch, settings, "plugin", "enable", DIST) == 0
    assert not _granted(settings)


def test_a_revoked_wheel_disables_the_installed_plugin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    signer, wheel = _installed_and_enabled(monkeypatch, settings, tmp_path)
    newer = FakeFetcher()
    _revoking(newer, signer, wheel, version=2, wheel_sha256=["sha256:" + wheel.sha256])
    assert _refresh(monkeypatch, settings, newer) == 0
    assert ("plugin.revoked", f"{KEY}:wheel_revoked") in db_rows(
        settings, "SELECT action, target FROM audit"
    )
    assert not _granted(settings)


def test_another_index_never_revokes_what_this_one_installed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    _signer, wheel = _installed_and_enabled(monkeypatch, settings, tmp_path)
    other = make_key()
    assert (
        run_cli(
            monkeypatch, settings, "plugin", "index", "add", OTHER_URL, "--pubkey", other.public_b64
        )
        == 0
    )
    hostile = FakeFetcher()
    _revoking(
        hostile, other, wheel, version=1, url=OTHER_URL,
        plugin_keys=[KEY], wheel_sha256=["sha256:" + wheel.sha256],
    )  # fmt: skip
    assert _refresh(monkeypatch, settings, hostile, OTHER_URL) == 0

    assert DIST in guard.read_state(guard.Layout.of(settings.data_dir)).enabled
    assert _granted(settings)
    assert ("plugin.revoked", f"{KEY}:plugin_revoked") not in db_rows(
        settings, "SELECT action, target FROM audit"
    )
