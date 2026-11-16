"""ENF-SEC-013, ENF-SEC-005, EF-EXP-020 (M4 L08a): the Komga credentials, sealed and shown nowhere.

The key of the Komga account is sealed by the `SecretVault` in the row of its target, like the
Kavita key, and read only by the core. It is never in a log line, in the audit, in the diagnostic
bundle, in the full open export, nor on a surface of the API: the attacks are a child, a member, a
key (read-only or not), another shelf (IDOR) and the admin himself, who is told that a key is set
and never what it is.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.export.targets.komga import (
    KomgaClient,
    KomgaKey,
    load_komga_key,
    store_komga_key,
)
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import export, identity, metadata
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from tests.auth_env import run_cli
from tests.fakes.komga import BASE_URL, FakeKomga, build_fetcher
from tests.fakes_clock import ManualClock
from tests.integration import test_fan_out_routes as routes_env
from tests.integration import test_l37_export_all as export_env
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_fan_out_routes import UNKNOWN, World
from tests.integration.test_l37_export_all import everything_under, export_to
from tests.komga_env import run
from tests.l50_env import seeded
from tests.redaction_support import forget_secret_values, synthetic_text
from tests.visibility_env import add_profile

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
export_target = metadata.tables["export_target"]
KEY = synthetic_text("komga-credentials-0001")
world = routes_env.world  # the fixtures of the routes and of the full export, reused
home = export_env.home


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    yield engine
    engine.dispose()


def _seal(engine: Engine, vault: SecretVault, key: str = KEY, *, name: str = "Komga") -> str:
    target_id = new_ulid()
    with engine.begin() as connection:
        registry.sync_registry(connection, vault.keyring)
        connection.execute(
            sa.insert(export_target).values(
                id=target_id, kind="komga", name=name, config={"url": BASE_URL}
            )
        )
        store_komga_key(vault, connection, target_id, KomgaKey(key))
    return target_id


@pytest.mark.req("ENF-SEC-013", "ENF-SEC-005", "EF-EXP-020")
def test_komga_credentials_sealed_never_logged_nor_exported(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Sealed at rest and opened by the vault alone; not in a repr, a log line, an error, a URL, a
    body, a stored row of the clear configuration, nor in the health document."""
    vault = SecretVault(Keyring.generate(NOW))
    target_id = _seal(engine, vault)
    with engine.connect() as connection:
        row = connection.execute(sa.select(export_target)).mappings().one()
        assert KEY.encode() not in bytes(row["secret_ct"])  # sealed
        assert all(KEY not in str(row[c]) for c in row.keys() if c != "secret_ct")  # noqa: SIM118
        opened = load_komga_key(vault, connection, target_id)
    assert opened.reveal() == KEY
    assert KEY not in repr(opened) and KEY not in str(opened) and KEY not in f"{opened!r:>40}"

    fake = FakeKomga(keys={KEY: True})
    client = KomgaClient(build_fetcher(fake), BASE_URL, opened, clock=ManualClock(NOW))
    forget_secret_values()
    opened = KomgaKey(KEY)  # a key built in a process registers itself for the scrubber
    client = KomgaClient(build_fetcher(fake), BASE_URL, opened, clock=ManualClock(NOW))
    with structlog.testing.capture_logs() as captured, caplog.at_level(logging.DEBUG):

        async def session() -> None:
            await client.probe()
            await client.list_libraries()
            await client.request_scan_for_path("/manga/Synthetic Series A")
            client.note_deletion("0SYNTHMANGA01")
            await client.flush_scans(now=True)

        run(session())
        revoked = KomgaClient(
            build_fetcher(FakeKomga(keys={})), BASE_URL, KomgaKey(KEY), clock=ManualClock(NOW)
        )
        with pytest.raises(Exception) as caught:  # a 401: the key was revoked
            run(revoked.list_libraries())
    assert KEY not in str(caught.value) + repr(caught.value) + repr(client) + repr(revoked)
    assert captured, "the client says what it does"
    assert KEY not in json.dumps(captured, default=str)
    assert KEY not in caplog.text
    assert fake.violations == []
    assert {r.headers["x-api-key"] for r in fake.requests} == {KEY}  # only in its header
    assert all(KEY not in r.raw_url and KEY.encode() not in r.body for r in fake.requests)


@pytest.mark.req("ENF-SEC-013", "ENF-SEC-005", "EF-EXP-020")
def test_komga_key_is_in_no_export_no_audit_no_bundle(
    home: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full open export, the audit table and the diagnostic bundle, one instance that holds a
    sealed Komga key (and a log that quotes it, as `docker compose logs` would)."""
    with seeded(home, tmp_path) as env:
        vault = env.inst.comp.vault()
        with write_transaction(env.inst.engine) as connection:
            target_id = new_ulid()
            registry.register_active_key(connection, vault.keyring)
            connection.execute(
                sa.insert(export_target).values(
                    id=target_id, kind="komga", name="Salon", config={"url": BASE_URL}
                )
            )  # a name no log line of the instance says: the last barrier of the bundle looks
            store_komga_key(vault, connection, target_id, KomgaKey(KEY))
        out = tmp_path / "out"
        export_to(env, out)
        dumped = everything_under(out)
        assert KEY not in dumped and "secret_ct" not in dumped
        assert not (out / "data" / "export_target.json").exists() or KEY not in (
            out / "data" / "export_target.json"
        ).read_text("utf-8")
        with env.inst.engine.connect() as connection:
            audit = connection.execute(sa.select(identity.audit)).all()
        assert KEY not in repr(audit)

        log = tmp_path / "komga.log"
        log.write_text(
            json.dumps({"event": "komga.call", "level": "debug", "logger": "shelfhold",
                        "timestamp": "2026-11-16T08:00:00Z", "route": "GET /api/v1/libraries",
                        "x_api_key": KEY, "message": f"key {KEY}"}) + "\n",
            encoding="utf-8",
        )  # fmt: skip
        forget_secret_values()
        bundle = tmp_path / "bundle" / "bundle.zip"
        bundle.parent.mkdir()
        code = run_cli(monkeypatch, home, "diagnostic", "bundle", "--out", str(bundle),
                       "--log-file", str(log))  # fmt: skip
        assert code == 0
        with zipfile.ZipFile(io.BytesIO(bundle.read_bytes())) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        assert members and all(KEY.encode() not in data for data in members.values())
        assert KEY.encode() not in bundle.read_bytes()


@pytest.mark.req("ENF-SEC-013", "EF-USR-024", "EF-EXP-047")
@pytest.mark.parametrize("role", ["child", "member"])
def test_komga_surfaces_refused_to_a_child_and_a_member_and_never_show_the_key(
    world: World,
    role: str,
) -> None:
    """The routes that list the bindings of a shelf show the tree and the server, never a key; a
    child and a member, even one the shelf is open to, are refused on every shelf (no oracle)."""
    _komga_on_world(world)
    extra = {"max_age_rating": 10} if role == "child" else {}
    client = add_profile(world.api, role, role, "10.0.0.5", **extra)
    for shelf in (world.family, world.adult, UNKNOWN):
        listed = client.get(world.path(shelf))
        assert listed.status_code == 403 and listed.json()["code"] == "forbidden", shelf
        assert KEY not in listed.text
    admin = world.api.client.get(world.path(world.adult))
    assert admin.status_code == 200 and KEY not in admin.text
    assert "secret" not in admin.text.lower() and "api_key" not in admin.text.lower()


@pytest.mark.req("ENF-SEC-013", "EF-USR-024")
def test_komga_surfaces_refused_to_an_api_key_read_only_or_not(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _komga_on_world(world)
    other = world.api.other_client("10.0.0.9")
    for flags in ((), ("--read-only",)):
        key = _create_key(monkeypatch, capsys, world.api.settings, *flags)
        listed = other.get(world.path(world.adult), headers=_bearer(key))
        assert listed.status_code in {401, 403}, flags
        assert KEY not in listed.text


@pytest.mark.req("ENF-SEC-013", "EF-USR-024", "EF-EXP-047")
def test_komga_idor_a_binding_of_one_shelf_never_shows_in_another(world: World) -> None:
    """A Komga bound to the adult shelf is not listed on the family shelf, and a listing carries
    the bindings of its own shelf only."""
    komga = _komga_on_world(world)
    (world.library / "komga").mkdir(exist_ok=True)
    created = world.api.post(
        world.path(world.adult), json={"target_id": komga, "root_subpath": "komga"}
    )
    assert created.status_code == 201, created.text
    own = world.api.client.get(world.path(world.adult)).json()["destinations"]
    other = world.api.client.get(world.path(world.family)).json()["destinations"]
    assert [d["id"] for d in own] == [created.json()["id"]] and other == []
    # the family shelf cannot take it: Komga keeps no account apart before the audit of L08b
    refused = world.api.post(
        world.path(world.family), json={"target_id": komga, "root_subpath": "komga"}
    )
    assert refused.status_code in {403, 409, 422}
    assert refused.json()["code"] == "shelf.destination_not_partitioned"
    assert KEY not in refused.text


def _komga_on_world(world: World) -> str:
    """A Komga target with a sealed key in the database of the web fixture."""
    vault = SecretVault(Keyring.generate(NOW))
    target_id = new_ulid()
    path_map = [{"local": str(world.library), "remote": "/manga", "mode": "rw"}]
    with write_transaction(world.engine) as connection:
        registry.sync_registry(connection, vault.keyring)
        connection.execute(
            sa.insert(export.export_target).values(
                id=target_id, kind="komga", name="Komga", path_map=path_map,
                config={"url": BASE_URL},
            )
        )  # fmt: skip
        store_komga_key(vault, connection, target_id, KomgaKey(KEY))
    return target_id
