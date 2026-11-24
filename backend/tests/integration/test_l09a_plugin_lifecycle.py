"""M6 L09a (EF-SRC-020, D15): the lifecycle of an installed third-party plugin.

Update is the admin's manual act, from the index the install is pinned to, and a pinned plugin
never moves: not by a refresh (which believes the index, never the installed files), not by
``update`` itself. The previous version is kept, and going back to it is a **reserved action** —
a fresh admin session or the host CLI, never a key. Uninstalling marks the bindings of the
plugin's sources « orphaned » without losing anything: the Work, its Edition, the Units, the
``export_item`` rows and the reading positions stay, and a reinstall of the same key at the same
signer reattaches what waited. At every change of version the attestation lapses (EF-EXP-065):
the word was given for code that is no longer the installed one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition, catalog, export, identity, reading
from tests.auth_env import (
    PASSWORD,
    AuthEnv,
    auth_env,
    db_rows,
    run_cli,
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

pytestmark = pytest.mark.req("EF-SRC-020")

URL = "https://index.example.org/index.json"
WHEEL_BASE = "https://index.example.org/wheels/"
DIST = "acme-plug"
KEY = "acme"
MODULE = "acme_plug"


def _expires(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wheel(tmp_path: Path, version: str) -> Any:
    return build_wheel(
        tmp_path,
        DIST,
        {f"{MODULE}/__init__.py": f"x = {version!r}\n"},
        entry=(KEY, f"{MODULE}:plugin"),
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
    state = settings.data_dir / "plugins" / "state.json"
    if not state.exists():
        return None
    installed = json.loads(state.read_text(encoding="utf-8")).get("installed", {})
    return installed.get(dist, {}).get("version")


def _install_row(settings: Settings, key: str = KEY) -> tuple[object, ...]:
    return db_rows(
        settings,
        "SELECT version, pinned, previous_version, previous_sha256 FROM plugin_install "
        "WHERE plugin_id = ?",
        key,
    )[0]


def _binding(settings: Settings, binding_id: str) -> tuple[object, ...]:
    return db_rows(
        settings, "SELECT active, disabled_reason FROM source_binding WHERE id = ?", binding_id
    )[0]


def _seed_library(settings: Settings, *, key: str = KEY) -> dict[str, str]:
    """A Work bound to the plugin's source, with one Unit, one export_item and one position:
    everything a binding holds when its plugin goes away."""
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            work_id, edition_id, item_id, binding_id = (new_ulid() for _ in range(4))
            unit_id, export_id, position_id, user_id = (new_ulid() for _ in range(4))
            connection.execute(
                sa.insert(catalog.work_kind)
                .values(key="manga", label_fr="manga", default_media="images")
                .prefix_with("OR IGNORE")
            )
            connection.execute(
                sa.insert(catalog.work).values(
                    id=work_id, kind="manga", title="Un titre", sort_title="un titre"
                )
            )
            connection.execute(
                sa.insert(catalog.edition).values(
                    id=edition_id, work_id=work_id, lang="fr", translation_type="official",
                    media="images",
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item_id, plugin_id=key, source_key=key, remote_id="series/acme-1"
                )
            )
            connection.execute(
                sa.insert(acquisition.source_binding).values(
                    id=binding_id, work_id=work_id, edition_id=edition_id,
                    source_item_id=item_id, priority=1, active=True,
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(catalog.unit).values(
                    id=unit_id, edition_id=edition_id, number_raw="1", number_norm="1"
                )
            )
            connection.execute(
                sa.insert(export.export_item).values(id=export_id, edition_id=edition_id,
                                                     kind="unit")
            )  # fmt: skip
            connection.execute(
                sa.insert(identity.user).values(id=user_id, username="reader", role="member")
            )
            connection.execute(
                sa.insert(reading.reading_position).values(
                    id=position_id, user_id=user_id, carrier_type="export_item",
                    carrier_id=export_id, content_hash="sha256:" + "1" * 64,
                    locator={"href": "a.xhtml", "progression": 0.5}, percent=0.5,
                    client_ts=datetime(2027, 1, 5, 9, 0, tzinfo=UTC),
                )
            )  # fmt: skip
    finally:
        engine.dispose()
    return {
        "work": work_id, "edition": edition_id, "item": item_id, "binding": binding_id,
        "unit": unit_id, "export": export_id, "position": position_id,
    }  # fmt: skip


def _held_rows(settings: Settings, ids: dict[str, str]) -> list[tuple[str, int]]:
    """What the binding holds, by table: nothing may be lost by an uninstall."""

    def _one(table: str, column: str, which: str) -> tuple[str, int]:
        return (
            table,
            len(db_rows(settings, f"SELECT id FROM {table} WHERE {column} = ?", ids[which])),  # noqa: S608
        )

    return [
        _one("work", "id", "work"),
        _one("edition", "id", "edition"),
        _one("source_item", "id", "item"),
        _one("unit", "id", "unit"),
        _one("export_item", "id", "export"),
        _one("reading_position", "id", "position"),
    ]


# -- update, pin, rollback, uninstall: one lifecycle, end to end --------------------------------


def test_plugin_update_pin_rollback_uninstall_orphans(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    signer = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", signer.public_b64)
        == 0
    )
    first, second, third = (_wheel(tmp_path, v) for v in ("1.0.0", "2.0.0", "3.0.0"))
    fetcher = FakeFetcher()
    _served(fetcher, signer, version=1, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, fetcher) == 0
    assert _state_version(settings) == "1.0.0"
    ids = _seed_library(settings)
    assert _binding(settings, ids["binding"]) == (1, None)

    # the admin's word for this version (EF-EXP-065): it lapses when the version changes
    assert (
        run_cli(monkeypatch, settings, "plugin", "attest", KEY, "content_trust", "publisher") == 0
    )
    assert db_rows(settings, "SELECT field FROM plugin_attestation WHERE plugin_key = ?", KEY)

    # the update: manual, from the index the install is pinned to
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: newer)
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) == 0
    assert _state_version(settings) == "2.0.0"
    assert _install_row(settings) == ("2.0.0", 0, "1.0.0", "sha256:" + first.sha256)
    assert db_rows(settings, "SELECT field FROM plugin_attestation WHERE plugin_key = ?", KEY) == []
    assert ("plugin.attestation_revoked", f"{KEY}@1.0.0:content_trust=publisher") in db_rows(
        settings, "SELECT action, target FROM audit"
    )
    assert _binding(settings, ids["binding"]) == (1, None)  # an update is not an uninstall

    # the pin: the version is frozen, and no refresh moves it either
    assert run_cli(monkeypatch, settings, "plugin", "pin", KEY) == 0
    assert _install_row(settings)[1] == 1
    pinned_index = FakeFetcher()
    _served(pinned_index, signer, version=3, wheel=third, plugin_version="3.0.0")
    monkeypatch.setattr(
        "shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: pinned_index
    )
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0
    assert _state_version(settings) == "2.0.0"  # a refresh believes the index, never the files
    assert not [u for u in pinned_index.calls if WHEEL_BASE in u]  # no wheel was even fetched
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) != 0
    assert ("plugin.update_refused", f"{KEY}:pinned") in db_rows(
        settings, "SELECT action, target FROM audit"
    )
    assert _state_version(settings) == "2.0.0"

    # the rollback: to the previous kept version, on the admin's explicit word
    assert run_cli(monkeypatch, settings, "plugin", "unpin", KEY) == 0
    assert run_cli(monkeypatch, settings, "plugin", "rollback", KEY) == 0
    assert _state_version(settings) == "1.0.0"
    assert _install_row(settings) == ("1.0.0", 0, None, None)  # no previous anymore: it is back
    assert ("plugin.rollback", KEY) in db_rows(settings, "SELECT action, target FROM audit")

    # the uninstall: the binding is orphaned, nothing it holds is lost
    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    assert _state_version(settings) is None
    assert _binding(settings, ids["binding"]) == (0, "orphaned")
    assert _held_rows(settings, ids) == [
        ("work", 1), ("edition", 1), ("source_item", 1), ("unit", 1),
        ("export_item", 1), ("reading_position", 1),
    ]  # fmt: skip

    # a reinstall of the same key at the same signer reattaches what waited
    again = FakeFetcher()
    _served(again, signer, version=4, wheel=first, plugin_version="1.0.0")
    assert _install(monkeypatch, settings, again) == 0
    assert _binding(settings, ids["binding"]) == (1, None)


# -- the uninstall orphans the binding without losing content ----------------------------------


def test_uninstalled_plugin_binding_orphaned_without_content_loss(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    wheel = _wheel(tmp_path, "1.0.0")
    assert (
        run_cli(
            monkeypatch,
            settings,
            "plugin",
            "install",
            str(wheel.path),
            "--sha256",
            wheel.sha256,
        )
        == 0
    )
    ids = _seed_library(settings)
    before = _held_rows(settings, ids)

    assert run_cli(monkeypatch, settings, "plugin", "remove", DIST) == 0
    assert _binding(settings, ids["binding"]) == (0, "orphaned")  # marked, not deleted
    assert _held_rows(settings, ids) == before  # nothing the binding holds was lost
    assert not (settings.data_dir / "plugins" / "site-packages" / MODULE).exists()

    # a reinstall of the same key reattaches the orphaned binding (the admin's own act is the
    # origin of a hand-given wheel, and the previous install was the admin's own too)
    assert (
        run_cli(
            monkeypatch,
            settings,
            "plugin",
            "install",
            str(wheel.path),
            "--sha256",
            wheel.sha256,
        )
        == 0
    )
    assert _binding(settings, ids["binding"]) == (1, None)


# -- a pinned plugin is never updated by a refresh ----------------------------------------------


def test_pinned_plugin_never_updated_by_refresh(
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
    assert run_cli(monkeypatch, settings, "plugin", "pin", KEY) == 0

    # the index now vouches for a newer version; the refresh believes it and installs nothing
    newer = FakeFetcher()
    _served(newer, signer, version=2, wheel=second, plugin_version="2.0.0")
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: newer)
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0
    assert _state_version(settings) == "1.0.0"
    assert not [u for u in newer.calls if WHEEL_BASE in u]

    # and the manual update refuses a pinned plugin, with its trace
    assert run_cli(monkeypatch, settings, "plugin", "update", KEY) != 0
    assert ("plugin.update_refused", f"{KEY}:pinned") in db_rows(
        settings, "SELECT action, target FROM audit"
    )
    assert _state_version(settings) == "1.0.0"
    assert _install_row(settings)[1] == 1  # the pin held: nothing moved


# -- the rollback is a reserved action ----------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.mark.req("EF-SRC-020", "EF-USR-024", "ENF-SEC-010")
def test_rollback_is_reserved(api: AuthEnv) -> None:
    """Going back to the previous kept version runs third-party code the index vouched for on
    the admin's word: a fresh admin session or the host CLI, never a key, never another role."""
    from tests.integration.test_child_surfaces import _routes
    from tests.integration.test_l07_signed_plugin_index import _key_of, _send

    assert "plugin.rollback" in RESERVED_ACTIONS
    base = "/api/v1/plugins"
    routes = {tuple(entry.split(" ", 1)) for entry in _routes(api.app)}
    assert ("POST", f"{base}/{{name}}/rollback") in routes
    assert ("POST", f"{base}/{{name}}/pin") in routes
    assert ("DELETE", f"{base}/{{name}}/pin") in routes

    # a fresh admin session may ask (the plugin here is unknown: a 404, not a refusal of the rule)
    api.login()
    fresh = api.post(f"{base}/{KEY}/rollback")
    assert fresh.status_code == 404, fresh.text

    # a stale session reauthenticates first
    api.clock.advance(timedelta(seconds=901))
    stale = api.post(f"{base}/{KEY}/rollback")
    assert stale.status_code == 401
    assert stale.json()["type"] == "/api/v1/problems/reauth-required"

    # never a key, however wide its scope
    admin_key = api.other_client("10.0.0.24")
    admin_key.headers["Authorization"] = f"Bearer {_key_of(api, 'admin')}"
    assert _send(api, admin_key, "POST", f"{base}/{KEY}/rollback").status_code == 403

    # and never another role
    for name, role, ip in (("kid", "child", "10.0.0.2"), ("ana", "member", "10.0.0.3")):
        auth = api.app.state.auth_gate.auth
        auth.profile_add(
            name, PASSWORD, role, **({"max_age_rating": 10} if role == "child" else {})
        )
        client = api.other_client(ip)
        assert api.login(name, client=client).status_code == 200
        assert _send(api, client, "POST", f"{base}/{KEY}/rollback").status_code == 403

    # the host CLI is the other credential: the rollback of the lifecycle test ran through it
