"""M6 L09a, the review's fixes: the lifecycle over HTTP, the word that lapses, the rollback
that fails.

- the ``update`` over HTTP moves the plugin and writes its audit line in the act's own
  transaction; the guard's refusal is the typed problem, never a 500, and an act whose line
  cannot be written is no act at all;
- the admin's word (EF-EXP-065) lapses at the rollback, and a reinstall does not bring it
  back: the word was given for code that is no longer the installed one;
- a rollback that fails puts the installed version back first — the plugin is never left
  uninstalled — and the reserved act can be taken again.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host import lifecycle
from tests.auth_env import ADMIN, AuthEnv, auth_env, db_rows, run_cli
from tests.integration.test_l09a_plugin_lifecycle import (
    KEY,
    URL,
    _binding,
    _install,
    _install_row,
    _seed_library,
    _served,
    _state_version,
    _wheel,
)
from tests.plugin_guard_env import Wheel
from tests.plugin_index_env import FakeFetcher, make_key

pytestmark = pytest.mark.req("EF-SRC-020")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _add_index(monkeypatch: pytest.MonkeyPatch, settings: Settings, pubkey: str) -> None:
    assert run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", pubkey) == 0


def _use(monkeypatch: pytest.MonkeyPatch, fetcher: FakeFetcher) -> None:
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: fetcher)


def _serving_http(monkeypatch: pytest.MonkeyPatch, fetcher: FakeFetcher) -> None:
    monkeypatch.setattr("shelfhold.main.index_fetcher", lambda _network: fetcher)


def _installed_1_0_0(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> tuple[Any, Any, FakeFetcher]:
    """An index-pinned install of 1.0.0 with the admin's word given for it, the 2.0.0 wheel,
    and the fetcher that serves it from the same signer."""
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    first, second = _wheel(tmp_path, "1.0.0"), _wheel(tmp_path, "2.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    assert (
        run_cli(monkeypatch, settings, "plugin", "attest", KEY, "content_trust", "publisher") == 0
    )
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    return first, second, newer


def _failing_install_wheel(monkeypatch: pytest.MonkeyPatch, fail_for: str) -> dict[str, bool]:
    """Make the guard's installer refuse one wheel (by its sha256): the refusal the recovery
    path and the routes must answer, never crash on."""
    real = lifecycle.install_wheel
    fail: dict[str, bool] = {"on": True}

    def install(layout: Any, wheel: Path, sha256: str, **kwargs: Any) -> Any:
        if fail["on"] and sha256 == fail_for:
            raise guard.PluginInstallError("install_failed", "the installer refused")
        return real(layout, wheel, sha256, **kwargs)

    monkeypatch.setattr("shelfhold.plugin_host.lifecycle.install_wheel", install)
    return fail


# -- the update and the rollback over HTTP ------------------------------------------------------


@pytest.mark.req("EF-SRC-020", "ENF-OBS-003")
def test_update_and_rollback_over_http(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Over the wire like from the host: the update installs what the pinned index vouches for
    and the rollback returns to the previous kept version, each with its audit line — the
    account that acted — and the word given for the old version lapses (EF-EXP-065)."""
    first, _second, newer = _installed_1_0_0(monkeypatch, settings, tmp_path)
    _serving_http(monkeypatch, newer)
    api.login()
    base = "/api/v1/plugins"
    answer = api.post(f"{base}/{KEY}/update")
    assert answer.status_code == 200, answer.text
    assert answer.json()["version"] == "2.0.0"
    assert _state_version(settings) == "2.0.0"
    assert _install_row(settings) == ("2.0.0", 0, "1.0.0", "sha256:" + first.sha256)
    actor = db_rows(settings, 'SELECT id FROM "user" WHERE username = ?', ADMIN)[0][0]
    assert ("plugin.update", KEY, actor) in db_rows(
        settings, "SELECT action, target, actor_id FROM audit"
    )
    assert db_rows(settings, "SELECT field FROM plugin_attestation WHERE plugin_key = ?", KEY) == []

    rolled = api.post(f"{base}/{KEY}/rollback")
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["version"] == "1.0.0"
    assert _install_row(settings) == ("1.0.0", 0, None, None)
    assert ("plugin.rollback", KEY, actor) in db_rows(
        settings, "SELECT action, target, actor_id FROM audit"
    )


@pytest.mark.req("EF-SRC-020", "ENF-OBS-003")
def test_update_refused_over_http_leaves_the_plugin_and_its_trace(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    _first, _second, newer = _installed_1_0_0(monkeypatch, settings, tmp_path)
    _serving_http(monkeypatch, newer)
    assert run_cli(monkeypatch, settings, "plugin", "pin", KEY) == 0
    api.login()
    answer = api.post(f"/api/v1/plugins/{KEY}/update")
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "plugin.update_refused"
    assert ("plugin.update_refused", f"{KEY}:pinned") in db_rows(
        settings, "SELECT action, target FROM audit"
    )
    assert _state_version(settings) == "1.0.0"


@pytest.mark.req("EF-SRC-020")
def test_update_over_http_answers_the_guards_refusal(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """A wheel the guard refuses to install is the typed problem over HTTP too, like the
    CLI's own refusal — never an unhandled 500 — and the installed plugin does not move."""
    _first, second, newer = _installed_1_0_0(monkeypatch, settings, tmp_path)
    _serving_http(monkeypatch, newer)
    _failing_install_wheel(monkeypatch, "sha256:" + second.sha256)
    api.login()
    answer = api.post(f"/api/v1/plugins/{KEY}/update")
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "plugin.update_refused"
    assert ("plugin.update_refused", f"{KEY}:install_failed") in db_rows(
        settings, "SELECT action, target FROM audit"
    )
    assert _state_version(settings) == "1.0.0"  # the old version back, nothing lost
    assert _install_row(settings)[0] == "1.0.0"


@pytest.mark.req("EF-SRC-020", "ENF-OBS-003")
def test_no_update_over_http_without_its_audit_line(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The line is the act's own (M6 L09a): an update whose audit line cannot be written is
    no update at all — the recorded install does not move, and no line says it did."""

    def refused(_connection: Any, *_args: Any, **_kwargs: Any) -> None:
        raise guard.PluginInstallError("install_failed", "the audit trail is the point")

    _first, _second, newer = _installed_1_0_0(monkeypatch, settings, tmp_path)
    _serving_http(monkeypatch, newer)
    monkeypatch.setattr("shelfhold.interfaces.http_api.plugins.write_audit", refused)
    api.login()
    answer = api.post(f"/api/v1/plugins/{KEY}/update")
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "plugin.update_refused"
    assert _install_row(settings) == ("1.0.0", 0, None, None)
    assert ("plugin.update", KEY) not in db_rows(settings, "SELECT action, target FROM audit")


@pytest.mark.req("EF-SRC-020", "ENF-OBS-003")
def test_pin_over_http_writes_one_line_for_one_act(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Pinning an already pinned plugin is no act: no second line, exactly like the CLI's
    callback, which writes the line in the act's own transaction or not at all."""
    _installed_1_0_0(monkeypatch, settings, tmp_path)
    api.login()
    assert api.post(f"/api/v1/plugins/{KEY}/pin").status_code == 200
    assert api.post(f"/api/v1/plugins/{KEY}/pin").status_code == 200
    rows = db_rows(settings, "SELECT action, target FROM audit WHERE action = 'plugin.pin'")
    assert rows == [("plugin.pin", KEY)]


# -- the word given for a version lapses at the rollback, not at a reinstall --------------------


@pytest.mark.req("EF-SRC-020", "EF-EXP-065")
def test_rollback_lapses_the_attestation(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The rollback changes the installed version: the admin's word was given for 2.0.0 and
    lapses with it (EF-EXP-065), with the trace of the revocation."""
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    first, second = _wheel(tmp_path, "1.0.0"), _wheel(tmp_path, "2.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    _use(monkeypatch, newer)
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) == 0
    assert (
        run_cli(monkeypatch, settings, "plugin", "attest", KEY, "content_trust", "publisher") == 0
    )

    assert run_cli(monkeypatch, settings, "plugin", "rollback", KEY) == 0
    assert _state_version(settings) == "1.0.0"
    assert db_rows(settings, "SELECT field FROM plugin_attestation WHERE plugin_key = ?", KEY) == []
    assert ("plugin.attestation_revoked", f"{KEY}@2.0.0:content_trust=publisher") in db_rows(
        settings, "SELECT action, target FROM audit"
    )


@pytest.mark.req("EF-SRC-020", "EF-EXP-065")
def test_reinstall_does_not_bring_the_attestation_back(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """A reinstall is new code, even of the same version: the word given to the previous
    install lapsed at the removal, and nothing resurrects it (EF-EXP-065)."""
    migrate(settings)
    wheel = _wheel(tmp_path, "1.0.0")
    assert (
        run_cli(monkeypatch, settings, "plugin", "install", str(wheel.path), "--sha256",
                wheel.sha256)
        == 0
    )  # fmt: skip
    assert (
        run_cli(monkeypatch, settings, "plugin", "attest", KEY, "content_trust", "publisher") == 0
    )
    assert run_cli(monkeypatch, settings, "plugin", "remove", "acme-plug") == 0

    # the reinstall: the word was given for the previous install, and it stays lapsed
    assert (
        run_cli(monkeypatch, settings, "plugin", "install", str(wheel.path), "--sha256",
                wheel.sha256)
        == 0
    )  # fmt: skip
    assert _state_version(settings) == "1.0.0"
    assert db_rows(settings, "SELECT field FROM plugin_attestation WHERE plugin_key = ?", KEY) == []
    assert ("plugin.attestation_revoked", f"{KEY}@1.0.0:content_trust=publisher") in db_rows(
        settings, "SELECT action, target FROM audit"
    )


# -- a rollback that fails puts the installed version back ---------------------------------------


@pytest.mark.req("EF-SRC-020")
def test_failed_rollback_puts_the_installed_version_back(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """A rollback whose install fails is a refusal that leaves nothing behind: the version
    that was installed is put back, the recorded state still answers for it, the bindings
    stay attached — and the reserved act can be taken again."""
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    first, second = _wheel(tmp_path, "1.0.0"), _wheel(tmp_path, "2.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    ids = _seed_library(settings)
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    _use(monkeypatch, newer)
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) == 0
    assert _binding(settings, ids["binding"]) == (1, None)

    fail = _failing_install_wheel(monkeypatch, "sha256:" + first.sha256)
    assert run_cli(monkeypatch, settings, "plugin", "rollback", KEY) != 0
    assert _state_version(settings) == "2.0.0"  # the version that was installed, back
    assert _install_row(settings) == ("2.0.0", 0, "1.0.0", "sha256:" + first.sha256)
    assert _binding(settings, ids["binding"]) == (1, None)
    assert ("plugin.rollback_refused", f"{KEY}:install_failed") in db_rows(
        settings, "SELECT action, target FROM audit"
    )

    fail["on"] = False
    assert run_cli(monkeypatch, settings, "plugin", "rollback", KEY) == 0
    assert _state_version(settings) == "1.0.0"
    assert _install_row(settings) == ("1.0.0", 0, None, None)


def _retagged(tmp_path: Path, wheel: Any, tag: str) -> Wheel:
    """The same wheel published under another tag section: the filename and the WHEEL metadata
    say the same thing, like a publisher's own build would."""
    with zipfile.ZipFile(wheel.path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    meta = next(name for name in files if name.endswith(".dist-info/WHEEL"))
    files[meta] = files[meta].replace(b"Tag: py3-none-any\n", f"Tag: {tag}\n".encode())
    path = tmp_path / wheel.path.name.replace("py3-none-any", tag)
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return Wheel(path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.req("EF-SRC-020")
def test_rollback_stages_the_kept_wheel_under_its_own_tags(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The kept wheel goes back under the name its own metadata answers for: the tags its
    publisher chose, never a made-up `py3-none-any` the wheel never declared."""
    migrate(settings)
    signer = make_key()
    _add_index(monkeypatch, settings, signer.public_b64)
    first = _retagged(tmp_path, _wheel(tmp_path, "1.0.0"), "cp313-none-any")
    second = _wheel(tmp_path, "2.0.0")
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    _use(monkeypatch, newer)
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) == 0

    assert run_cli(monkeypatch, settings, "plugin", "rollback", KEY) == 0
    assert _state_version(settings) == "1.0.0"
