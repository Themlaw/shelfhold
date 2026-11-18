"""EF-EXP-020, EF-EXP-022, EF-LEC-014, EF-USR-024, ENF-SEC-001, ENF-SEC-005, ENF-SEC-013 (M4 L46):
the wiring of the destinations that nothing created yet, and the switch of the progress sync.

`target add komga` and `target add cwa` stand on the model of `target add kavita`: the secret comes
from a file, is sealed in the row of its target and is never read back; the address goes through the
guarded Fetcher; the folders are the path map. The switch of `sync_progress` of a binding is a
reserved action (a fresh admin session or the host's command line, never a key, a member or a
child), journaled, and refused with a code for a destination that cannot synchronise.
"""

# ruff: noqa: F811  (the `world` fixture of L06a is imported, then asked for by name)
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.targets.komga import load_komga_key
from shelfhold.headless import HeadlessError, wiring
from shelfhold.headless.adapters import HttpKomgaProbe
from shelfhold.headless.errors import TargetConflict, TargetExchangeFailed
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fetcher import IntegrationFetcher, LanTargets
from shelfhold.infrastructure.fetcher.network import Network
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity
from shelfhold.runtime_jobs import progress_destinations
from tests.auth_env import run_cli
from tests.fakes.komga import (
    API_KEY,
    BASE_URL,
    OTHER_KEY,
    UNKNOWN_KEY,
    FakeKomga,
    build_fetcher,
)
from tests.fakes_fetcher import FakeResolver
from tests.headless_env import Headless, headless, write_secret
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_fan_out_routes import UNKNOWN, World, world  # noqa: F401
from tests.visibility_env import add_profile

KOMGA = "/api/v1/targets/komga"
CWA = "/api/v1/targets/cwa"


def _sync(destination_id: str) -> str:
    return f"/api/v1/destinations/{destination_id}/sync-progress"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
        "SHELFHOLD_DISK_PAUSE_GIB": "0",
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
        "SHELFHOLD_LIBRARY_DIR": str(tmp_path / "library"),
    }


def _probe(fake: FakeKomga) -> HttpKomgaProbe:
    """The production adapter, its only reachable host being the simulated Komga (a declared LAN
    target): the guard and the header rules are the real ones."""
    return HttpKomgaProbe(lambda _url: build_fetcher(fake))


def _undeclared_probe(fake: FakeKomga) -> HttpKomgaProbe:
    """The same adapter with no declared LAN target: a private address is out of reach."""
    resolver = FakeResolver({"komga.lan": ["192.168.1.60"]})

    def factory(_url: str) -> IntegrationFetcher:
        return IntegrationFetcher(resolver, lan_targets=LanTargets.empty(),
                                  transport=fake.transport)  # fmt: skip

    return HttpKomgaProbe(factory)


@pytest.fixture
def komga_env(settings: Settings) -> Iterator[tuple[Headless, FakeKomga]]:
    fake = FakeKomga(keys={API_KEY: True, OTHER_KEY: False})
    with headless(settings, komga=_probe(fake)) as env:
        yield env, fake


def _rows(env: Headless, kind: str) -> list[Any]:
    with env.engine.connect() as connection:
        return list(
            connection.execute(
                sa.select(export.export_target).where(export.export_target.c.kind == kind)
            )
        )


def _audit(engine: sa.Engine, action: str) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(sa.select(identity.audit).where(identity.audit.c.action == action))
        )


# ------------------------------------------------------------------------------ target add komga


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013", "ENF-SEC-005")
def test_target_add_komga_probes_seals_and_stores_no_clear_secret(
    komga_env: tuple[Headless, FakeKomga],
) -> None:
    env, fake = komga_env
    added = env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY)
    assert added.created
    (row,) = _rows(env, "komga")
    assert row.config == {"url": BASE_URL} and row.version_seen == fake.version
    assert fake.violations == [] and [r.path for r in fake.requests] == ["/actuator/info"]
    assert {r.headers["x-api-key"] for r in fake.requests} == {API_KEY}  # in the header only
    assert all(API_KEY not in r.raw_url for r in fake.requests)
    with env.engine.connect() as connection:
        opened = load_komga_key(env.vault, connection, row.id)
    assert opened.reveal() == API_KEY
    assert API_KEY not in env.dump()  # sealed at rest, nowhere in clear
    assert [a.target for a in _audit(env.engine, "target.add_komga")] != []


@pytest.mark.req("EF-EXP-020")
def test_target_add_komga_is_idempotent_and_refuses_a_name_taken_elsewhere(
    komga_env: tuple[Headless, FakeKomga], tmp_path: Path
) -> None:
    env, _fake = komga_env
    first = env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY)
    again = env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY)
    assert not again.created and again.id == first.id and len(_rows(env, "komga")) == 1
    with pytest.raises(TargetConflict):
        env.admin.target_add_komga(name="Komga", url="http://other.lan:25600", api_key=API_KEY)
    drop = tmp_path / "ingest"
    drop.mkdir()
    pmap = [{"local": str(drop), "remote": "/ingest", "mode": "rw"}]
    env.admin.target_add_cwa(name="Drop", path_map=pmap)
    with pytest.raises(TargetConflict):  # a drop box cannot take the name of a Komga
        env.admin.target_add_cwa(name="Komga", path_map=pmap)
    other = tmp_path / "other"
    other.mkdir()
    again = env.admin.target_add_komga(  # the same Komga, a new map: the map follows, no new row
        name="Komga", url=BASE_URL, api_key=API_KEY,
        path_map=[{"local": str(other), "remote": "/other", "mode": "rw"}],
    )  # fmt: skip
    assert not again.created and len(_rows(env, "komga")) == 1
    with pytest.raises(TargetConflict):  # a name that is another kind of target
        env.admin.target_add_komga(name="Drop", url=BASE_URL, api_key=API_KEY)


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013")
@pytest.mark.parametrize("key", [OTHER_KEY, UNKNOWN_KEY])
def test_a_key_komga_refuses_stores_nothing(
    komga_env: tuple[Headless, FakeKomga], key: str
) -> None:
    """A key that is not an administrator's (scan and `empty-trash` need that role), or unknown."""
    env, _fake = komga_env
    with pytest.raises(TargetExchangeFailed) as refused:
        env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=key)
    assert key not in str(refused.value)
    assert _rows(env, "komga") == [] and _audit(env.engine, "target.add_komga") == []


@pytest.mark.req("EF-EXP-020", "ENF-SEC-001")
def test_a_private_address_is_refused_without_the_setting(settings: Settings) -> None:
    """The address goes through the guarded Fetcher: no request leaves for a private address that
    nobody declared, and nothing is stored."""
    fake = FakeKomga()
    with headless(settings, komga=_undeclared_probe(fake)) as env:
        with pytest.raises(TargetExchangeFailed):
            env.admin.target_add_komga(name="Komga", url="http://komga.lan:25600", api_key=API_KEY)
        assert fake.requests == [] and _rows(env, "komga") == []
        for url in (
            "http://203.0.113.9:25600",
            f"http://user:{OTHER_KEY}@komga.lan:25600",
            "ftp://komga.lan",
        ):
            with pytest.raises(HeadlessError):
                env.admin.target_add_komga(name="Komga", url=url, api_key=API_KEY)
        assert fake.requests == [] and _rows(env, "komga") == []


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013")
def test_an_empty_or_unreadable_key_is_refused_before_any_request(
    komga_env: tuple[Headless, FakeKomga],
) -> None:
    env, fake = komga_env
    with pytest.raises(HeadlessError):
        env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key="")
    assert fake.requests == [] and _rows(env, "komga") == []


# --------------------------------------------------------------------------------- target add cwa


@pytest.mark.req("EF-EXP-022", "ENF-SEC-001")
def test_target_add_cwa_keeps_no_secret_and_needs_writable_folders(
    komga_env: tuple[Headless, FakeKomga], tmp_path: Path
) -> None:
    env, fake = komga_env
    drop = tmp_path / "library" / "cwa-ingest"
    drop.mkdir(parents=True)
    path_map = [{"local": str(drop), "remote": "/cwa-ingest", "mode": "rw"}]
    added = env.admin.target_add_cwa(name="Drop", path_map=path_map, actor=None)
    assert added.created
    (row,) = _rows(env, "cwa")
    assert row.config == {} and row.access_partitioning == "none"
    assert [e["mode"] for e in row.path_map] == ["rw"] and fake.requests == []
    assert not env.admin.target_add_cwa(name="Drop", path_map=path_map).created
    with pytest.raises(HeadlessError):  # a drop box is written: read-only folders are refused
        env.admin.target_add_cwa(
            name="Ro", path_map=[{"local": str(drop), "remote": "/x", "mode": "adopt_ro"}]
        )
    assert len(_rows(env, "cwa")) == 1


# ------------------------------------------------------------------------------------- the CLI


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013")
def test_cli_target_add_komga_reads_the_key_from_a_file_never_from_argv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    komga_env: tuple[Headless, FakeKomga],
    tmp_path: Path,
) -> None:
    from shelfhold.interfaces.cli.registry import build_parser

    env, fake = komga_env
    for argv in (
        ["target", "add", "komga", "--url", BASE_URL, "--api-key", "x"],
        ["target", "add", "komga", "--url", BASE_URL, "--password", "x"],
    ):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)
    monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
    key_file = write_secret(tmp_path, "komga.key", API_KEY + "\n")
    code = run_cli(monkeypatch, env.settings, "target", "add", "komga", "--url", BASE_URL,
                   "--name", "salon", "--password-file", str(key_file))  # fmt: skip
    out = capsys.readouterr()
    assert code == 0 and API_KEY not in out.out + out.err
    assert [r.name for r in _rows(env, "komga")] == ["salon"] and fake.violations == []
    stdin = run_cli(monkeypatch, env.settings, "target", "add", "komga", "--url", BASE_URL,
                    "--name", "bis", "--password-file", "-", stdin=API_KEY + "\n")  # fmt: skip
    assert stdin == 0 and len(_rows(env, "komga")) == 2
    refused = run_cli(monkeypatch, env.settings, "target", "add", "komga", "--url", BASE_URL,
                      "--name", "ter", "--password-file", str(tmp_path / "missing"))  # fmt: skip
    assert refused != 0 and len(_rows(env, "komga")) == 2


@pytest.mark.req("EF-EXP-022")
def test_cli_target_add_cwa(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    komga_env: tuple[Headless, FakeKomga],
    tmp_path: Path,
) -> None:
    env, _fake = komga_env
    monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
    drop = tmp_path / "library" / "ingest"
    drop.mkdir(parents=True)
    pmap = f'[{{"local": "{drop}", "remote": "/ingest", "mode": "rw"}}]'
    code = run_cli(monkeypatch, env.settings, "target", "add", "cwa", "--name", "drop",
                   "--path-map", pmap)  # fmt: skip
    assert code == 0 and [r.name for r in _rows(env, "cwa")] == ["drop"]
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------------------ the HTTP creation routes


@pytest.fixture
def komga_http(monkeypatch: pytest.MonkeyPatch) -> FakeKomga:
    fake = FakeKomga(keys={API_KEY: True, OTHER_KEY: False})
    # the application may have opened its configuration facade already: patch the network itself
    monkeypatch.setattr(
        Network, "integration_fetcher", lambda _self, _url, *_a, **_k: build_fetcher(fake)
    )
    return fake


def _count(world: World, kind: str) -> int:
    with world.engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT count(*) FROM export_target WHERE kind = :k"), {"k": kind}
        ).scalar_one()


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013", "ENF-SEC-005")
def test_an_admin_creates_a_komga_target_and_the_key_never_comes_back(
    world: World, komga_http: FakeKomga
) -> None:
    body = {"name": "Salon", "url": BASE_URL, "api_key": API_KEY}
    created = world.api.post(KOMGA, json=body)
    assert created.status_code == 201, created.text
    assert API_KEY not in created.text and set(created.json()) == {"id"}
    with world.engine.connect() as connection:
        row = connection.execute(
            sa.select(export.export_target).where(export.export_target.c.id == created.json()["id"])
        ).one()
        audit = connection.execute(
            sa.select(identity.audit).where(identity.audit.c.action == "target.add_komga")
        ).all()
    assert row.kind == "komga" and API_KEY not in str(row._mapping) and len(audit) == 1
    assert API_KEY not in str(audit)
    wrong = world.api.post(KOMGA, json={**body, "name": "Autre", "api_key": OTHER_KEY})
    assert wrong.status_code == 502 and wrong.json()["code"] == "target.exchange_failed"
    assert OTHER_KEY not in wrong.text and _count(world, "komga") == 1
    for target in (
        world.api.client.get("/api/v1/status").text,
        world.api.post(KOMGA, json=body).text,
    ):
        assert API_KEY not in target


@pytest.mark.req("EF-EXP-022")
def test_an_admin_creates_a_cwa_target(world: World) -> None:
    drop = world.library.parent / "ingest"
    drop.mkdir(exist_ok=True)
    body = {"name": "Drop", "path_map": [{"local": str(drop), "remote": "/ingest", "mode": "rw"}]}
    created = world.api.post(CWA, json=body)
    assert created.status_code == 201, created.text
    assert _count(world, "cwa") == 1


@pytest.mark.req("EF-EXP-020", "EF-EXP-022", "EF-USR-024", "ENF-SEC-010", "ENF-SEC-013")
@pytest.mark.parametrize("role", ["child", "member"])
def test_a_child_or_a_member_creates_no_komga_nor_cwa_target(
    world: World, komga_http: FakeKomga, role: str
) -> None:
    extra = {"max_age_rating": 10} if role == "child" else {}
    client = add_profile(world.api, role, role, "10.0.0.5", **extra)
    headers = {"X-CSRF-Token": world.api.csrf(client)}
    komga = {"name": "Salon", "url": BASE_URL, "api_key": API_KEY}
    cwa = {
        "name": "Drop",
        "path_map": [{"local": str(world.library), "remote": "/x", "mode": "rw"}],
    }
    for route, body in ((KOMGA, komga), (CWA, cwa)):
        refused = client.post(route, json=body, headers=headers)
        assert refused.status_code == 403 and refused.json()["code"] == "forbidden", route
        assert API_KEY not in refused.text
    assert komga_http.requests == []  # nothing left for the server
    assert _count(world, "komga") == 0 and _count(world, "cwa") == 0


@pytest.mark.req("EF-EXP-020", "EF-EXP-022", "EF-USR-024", "ENF-SEC-010", "ENF-SEC-013")
def test_an_api_key_creates_no_komga_nor_cwa_target(
    world: World,
    komga_http: FakeKomga,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = world.api.other_client("10.0.0.9")
    komga = {"name": "Salon", "url": BASE_URL, "api_key": API_KEY}
    cwa = {
        "name": "Drop",
        "path_map": [{"local": str(world.library), "remote": "/x", "mode": "rw"}],
    }
    for flags in ((), ("--read-only",)):
        key = _create_key(monkeypatch, capsys, world.api.settings, *flags)
        for route, body in ((KOMGA, komga), (CWA, cwa)):
            refused = other.post(route, json=body, headers=_bearer(key))
            assert refused.status_code in {401, 403}, (route, flags)
            assert API_KEY not in refused.text
    assert komga_http.requests == []
    assert _count(world, "komga") == 0 and _count(world, "cwa") == 0


@pytest.mark.req("EF-EXP-020", "ENF-SEC-001")
def test_the_komga_route_refuses_a_private_address_nobody_declared(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeKomga()
    undeclared = _undeclared_probe(fake)
    monkeypatch.setattr(
        Network, "integration_fetcher",
        lambda _self, url, *_a, **_k: undeclared._fetchers(url),
    )  # fmt: skip
    refused = world.api.post(KOMGA, json={"name": "Salon", "url": "http://komga.lan:25600",
                                          "api_key": API_KEY})  # fmt: skip
    assert refused.status_code in {422, 502} and fake.requests == []
    assert API_KEY not in refused.text and _count(world, "komga") == 0


# ---------------------------------------------------------------------------- the progress switch


def _bind(world: World, shelf: str, sub: str, target: str, **body: Any) -> str:
    made = world.bind(shelf, sub, target=target, **body)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _flag(world: World, destination_id: str) -> bool:
    with world.engine.connect() as connection:
        return bool(
            connection.execute(
                sa.select(export.shelf_destination.c.sync_progress).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar_one()
        )


@pytest.mark.req("EF-LEC-014", "EF-EXP-047", "EF-USR-024")
def test_an_admin_turns_the_progress_sync_of_a_kavita_binding_on_and_off(world: World) -> None:
    destination = _bind(world, world.adult, "one", world.server)
    assert not _flag(world, destination) and progress_destinations(world.engine) == []
    on = world.api.post(_sync(destination), json={"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json() == {"destination_id": destination, "sync_progress": True}
    assert _flag(world, destination) and progress_destinations(world.engine) == [destination]
    again = world.api.post(_sync(destination), json={"enabled": True})
    assert again.status_code == 200 and _flag(world, destination)
    off = world.api.post(_sync(destination), json={"enabled": False})
    assert off.status_code == 200 and off.json()["sync_progress"] is False
    assert not _flag(world, destination) and progress_destinations(world.engine) == []
    rows = _audit(world.engine, "destination.sync_progress")
    assert [r.target for r in rows] == [f"{destination} on", f"{destination} off"]  # a change each
    listed = world.api.client.get("/api/v1/destinations").json()["items"]
    assert {i["id"]: i["sync_progress"] for i in listed} == {destination: False}


@pytest.mark.req("EF-LEC-014", "EF-EXP-047")
def test_the_pass_takes_the_binding_that_was_switched_on_and_only_that_one(world: World) -> None:
    first = _bind(world, world.adult, "one", world.server)
    second = _bind(world, world.adult, "two", world.other)
    assert world.api.post(_sync(second), json={"enabled": True}).status_code == 200
    assert progress_destinations(world.engine) == [second]
    assert world.api.post(_sync(first), json={"enabled": True}).status_code == 200
    assert progress_destinations(world.engine) == sorted([first, second])


def _komga_binding(world: World, kind: str) -> str:
    target = new_ulid()
    path_map = [{"local": str(world.library), "remote": "/manga", "mode": "rw"}]
    with write_transaction(world.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=target, kind=kind, name=f"T-{kind}", path_map=path_map,
                config={"url": BASE_URL} if kind == "komga" else {},
            )
        )  # fmt: skip
    extra = {"transport": "dropbox"} if kind == "cwa" else {}
    return _bind(world, world.adult, f"sub-{kind}", target, **extra)


@pytest.mark.req("EF-LEC-014", "EF-EXP-047")
@pytest.mark.parametrize("kind", ["komga", "cwa", "native_folder"])
def test_a_destination_that_cannot_synchronise_is_refused_with_a_clear_code(
    world: World, kind: str
) -> None:
    destination = _komga_binding(world, kind)
    refused = world.api.post(_sync(destination), json={"enabled": True})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "destination.sync_unsupported"
    assert not _flag(world, destination) and progress_destinations(world.engine) == []
    assert _audit(world.engine, "destination.sync_progress") == []
    off = world.api.post(_sync(destination), json={"enabled": False})  # nothing to turn off
    assert off.status_code == 200 and off.json()["sync_progress"] is False


@pytest.mark.req("EF-LEC-014")
def test_an_unknown_destination_is_a_404(world: World) -> None:
    refused = world.api.post(_sync(UNKNOWN), json={"enabled": True})
    assert refused.status_code == 404 and refused.json()["code"] == "target.unknown"


@pytest.mark.req("EF-LEC-014", "EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("role", ["child", "member"])
def test_a_child_or_a_member_switches_nothing(world: World, role: str) -> None:
    destination = _bind(world, world.adult, "one", world.server)
    extra = {"max_age_rating": 10} if role == "child" else {}
    client = add_profile(world.api, role, role, "10.0.0.5", **extra)
    headers = {"X-CSRF-Token": world.api.csrf(client)}
    for target in (destination, UNKNOWN):  # same answer: no oracle on the identifiers of others
        refused = client.post(_sync(target), json={"enabled": True}, headers=headers)
        assert refused.status_code == 403 and refused.json()["code"] == "forbidden", target
    assert not _flag(world, destination) and _audit(world.engine, "destination.sync_progress") == []


@pytest.mark.req("EF-LEC-014", "EF-USR-024", "ENF-SEC-010")
def test_an_api_key_switches_nothing_read_only_or_not(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = _bind(world, world.adult, "one", world.server)
    other = world.api.other_client("10.0.0.9")
    for flags in ((), ("--read-only",)):
        key = _create_key(monkeypatch, capsys, world.api.settings, *flags)
        refused = other.post(_sync(destination), json={"enabled": True}, headers=_bearer(key))
        assert refused.status_code in {401, 403}, flags
    assert not _flag(world, destination) and _audit(world.engine, "destination.sync_progress") == []


# ------------------------------------------------------- the service and the host's command line


@pytest.mark.req("EF-LEC-014", "EF-EXP-047")
def test_the_service_switch_is_audited_and_the_cli_does_the_same(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    komga_env: tuple[Headless, FakeKomga],
) -> None:
    env, _fake = komga_env
    target = new_ulid()
    shelf, destination = new_ulid(), new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(id=target, kind="kavita", name="K")
        )
        connection.execute(sa.insert(export.shelf).values(id=shelf, name="S", phrase="p"))
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination,
                shelf_id=shelf,
                target_id=target,
                transport="tree",
                egress="foyer",
                root_subpath="a",
                template="t",
                template_origin="default@1",
            )
        )
    monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
    assert run_cli(monkeypatch, env.settings, "target", "sync-progress", destination, "--on") == 0
    with env.engine.connect() as connection:
        assert connection.execute(sa.select(export.shelf_destination.c.sync_progress)).scalar_one()
    first = _audit(env.engine, "destination.sync_progress")[0]
    assert first.target.startswith(destination)
    assert run_cli(monkeypatch, env.settings, "target", "sync-progress", destination, "--off") == 0
    with env.engine.connect() as connection:
        assert not connection.execute(
            sa.select(export.shelf_destination.c.sync_progress)
        ).scalar_one()
    capsys.readouterr()
    assert run_cli(monkeypatch, env.settings, "target", "sync-progress", "nope", "--on") != 0
    assert "target.unknown" in capsys.readouterr().err


@pytest.mark.req("EF-LEC-014")
def test_the_cli_names_cause_and_action_for_a_destination_that_cannot_synchronise(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    komga_env: tuple[Headless, FakeKomga],
) -> None:
    env, _fake = komga_env
    target, shelf, destination = new_ulid(), new_ulid(), new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(id=target, kind="komga", name="K", config={})
        )
        connection.execute(sa.insert(export.shelf).values(id=shelf, name="S", phrase="p"))
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination,
                shelf_id=shelf,
                target_id=target,
                transport="tree",
                egress="foyer",
                root_subpath="a",
                template="t",
                template_origin="default@1",
            )
        )
    monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
    assert run_cli(monkeypatch, env.settings, "target", "sync-progress", destination, "--on") != 0
    err = capsys.readouterr().err
    assert "destination.sync_unsupported" in err
    with env.engine.connect() as connection:
        assert not connection.execute(
            sa.select(export.shelf_destination.c.sync_progress)
        ).scalar_one()


# --------------------------------------------------------------- the socket of the running server


@pytest.mark.req("EF-EXP-020", "EF-EXP-022", "EF-LEC-014", "EF-USR-024")
def test_the_socket_methods_create_the_targets_and_switch_the_sync(
    komga_env: tuple[Headless, FakeKomga], tmp_path: Path
) -> None:
    """The host's command line talks to the running server over the socket: the three methods
    answer with the same shapes, and a refusal crosses it as its catalog code."""
    from shelfhold.identity.public import local_auth
    from shelfhold.infrastructure.local_socket import RpcFailure, call
    from shelfhold.interfaces.local_cli import IdentityDispatcher, SocketHeadless
    from tests.local_socket_env import serving

    env, _fake = komga_env
    drop = tmp_path / "library" / "socket-ingest"
    drop.mkdir(parents=True)
    pmap = [{"local": str(drop), "remote": "/socket-ingest", "mode": "rw"}]
    sock = tmp_path / "s.sock"
    auth = local_auth(env.engine, env.settings)
    with serving(sock, IdentityDispatcher(auth, headless=env.admin)):
        remote = SocketHeadless(sock)
        komga = remote.target_add_komga(name="Salon", url=BASE_URL, api_key=API_KEY)
        cwa = remote.target_add_cwa(name="Drop", path_map=pmap)
        assert komga.created and cwa.created and API_KEY not in repr((komga, cwa))
        assert not remote.target_add_cwa(name="Drop", path_map=pmap).created
        raw = call(sock, "target.add_komga", {"name": "Salon", "url": BASE_URL, "api_key": API_KEY})
        assert API_KEY not in repr(raw)
        with pytest.raises(RpcFailure) as refused:  # the key of an account that is not an admin
            call(sock, "target.add_komga", {"name": "Autre", "url": BASE_URL, "api_key": OTHER_KEY})
        assert (refused.value.data or {}).get("code") == "target.exchange_failed"
        target, shelf, destination = new_ulid(), new_ulid(), new_ulid()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(export.export_target).values(id=target, kind="kavita", name="K")
            )
            connection.execute(sa.insert(export.shelf).values(id=shelf, name="S", phrase="p"))
            connection.execute(
                sa.insert(export.shelf_destination).values(
                    id=destination, shelf_id=shelf, target_id=target, transport="tree",
                    egress="foyer", root_subpath="a", template="t", template_origin="default@1",
                )
            )  # fmt: skip
        on = remote.destination_sync_progress(destination_id=destination, enabled=True)
        assert on.sync_progress and progress_destinations(env.engine) == [destination]
        off = remote.destination_sync_progress(destination_id=destination, enabled=False)
        assert not off.sync_progress and progress_destinations(env.engine) == []
        with pytest.raises(RpcFailure) as unknown:
            call(sock, "destination.sync_progress", {"destination_id": UNKNOWN, "enabled": True})
        assert (unknown.value.data or {}).get("code") == "target.unknown"


@pytest.mark.req("EF-EXP-020", "EF-EXP-022")
def test_the_facade_refuses_what_it_cannot_do(settings: Settings, tmp_path: Path) -> None:
    """No probe wired, no folder given, a name that is another kind, a probe that cannot even
    open its connection: each is a refusal with its code, and nothing is stored."""
    from shelfhold.infrastructure.fetcher import FetchError

    drop = tmp_path / "ingest"
    drop.mkdir()
    pmap = [{"local": str(drop), "remote": "/ingest", "mode": "rw"}]

    def no_route(_url: str) -> IntegrationFetcher:
        raise FetchError("refused")

    with headless(settings) as bare:  # no Komga probe at all
        with pytest.raises(TargetExchangeFailed):
            bare.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY)
        with pytest.raises(HeadlessError):  # a drop box with no folder to write
            bare.admin.target_add_cwa(name="Drop", path_map=None)
        bare.admin.target_add_cwa(name="Drop", path_map=pmap)
        with pytest.raises(TargetConflict):  # the name is a drop box, not a Komga
            bare.admin.target_add_komga(name="Drop", url=BASE_URL, api_key=API_KEY)
    with headless(settings, komga=HttpKomgaProbe(no_route)) as closed:
        with pytest.raises(TargetExchangeFailed):
            closed.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY)
        assert _rows(closed, "komga") == []


@pytest.mark.req("EF-EXP-020", "EF-EXP-022")
def test_cli_refuses_a_path_map_that_is_not_json_and_says_a_target_is_already_there(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    komga_env: tuple[Headless, FakeKomga],
    tmp_path: Path,
) -> None:
    env, _fake = komga_env
    monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
    key_file = write_secret(tmp_path, "komga.key", API_KEY + "\n")
    base = ["target", "add", "komga", "--url", BASE_URL, "--password-file", str(key_file)]
    assert run_cli(monkeypatch, env.settings, *base, "--path-map", "not json") != 0
    assert run_cli(monkeypatch, env.settings, *base, "--path-map", '{"a": 1}') != 0
    assert run_cli(monkeypatch, env.settings, "target", "add", "cwa", "--path-map", "[") != 0
    assert _rows(env, "komga") == [] and _rows(env, "cwa") == []
    capsys.readouterr()
    assert run_cli(monkeypatch, env.settings, *base) == 0
    assert run_cli(monkeypatch, env.settings, *base) == 0  # already there: nothing to do
    assert "déjà présente" in capsys.readouterr().out and len(_rows(env, "komga")) == 1
    drop = tmp_path / "library" / "ingest"
    drop.mkdir(parents=True)
    cwa = ["target", "add", "cwa", "--path-map",
           f'[{{"local": "{drop}", "remote": "/ingest", "mode": "rw"}}]']  # fmt: skip
    assert run_cli(monkeypatch, env.settings, *cwa) == 0
    assert run_cli(monkeypatch, env.settings, *cwa) == 0 and len(_rows(env, "cwa")) == 1


@pytest.mark.req("EF-EXP-022", "ENF-SEC-001")
def test_the_cwa_route_refuses_a_read_only_folder(world: World) -> None:
    body = {"name": "Drop", "path_map": [{"local": str(world.library), "remote": "/x",
                                          "mode": "adopt_ro"}]}  # fmt: skip
    refused = world.api.post(CWA, json=body)
    assert refused.status_code == 422 and refused.json()["code"] == "config.invalid_value"
    assert _count(world, "cwa") == 0


# ------------------------------------------------- #342: path map and key of an existing target


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013", "EF-USR-024")
def test_a_new_komga_key_is_probed_sealed_and_journaled_and_the_same_key_writes_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    from tests.redaction_support import synthetic_text

    fresh = synthetic_text("komga-fake-rotated-0342")
    fake = FakeKomga(keys={API_KEY: True, OTHER_KEY: False, fresh: True})
    with headless(settings, komga=_probe(fake)) as env:
        first = env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY)
        before = len(_audit(env.engine, "target.rotate_komga_key"))
        same = env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY)
        assert not same.created and len(_audit(env.engine, "target.rotate_komga_key")) == before
        with pytest.raises(TargetExchangeFailed):  # a key Komga refuses: the old one stays
            env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=OTHER_KEY)
        with env.engine.connect() as connection:
            assert load_komga_key(env.vault, connection, first.id).reveal() == API_KEY
        rotated = env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=fresh)
        assert not rotated.created and rotated.id == first.id
        with env.engine.connect() as connection:
            assert load_komga_key(env.vault, connection, first.id).reveal() == fresh
        assert len(_audit(env.engine, "target.rotate_komga_key")) == before + 1
        assert API_KEY not in env.dump() and fresh not in env.dump()
        assert fake.violations == []


@pytest.mark.req("EF-EXP-020", "EF-USR-024")
def test_a_path_map_changed_through_an_add_door_leaves_its_line(
    settings: Settings, tmp_path: Path
) -> None:
    fake = FakeKomga()
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    one = [{"local": str(first), "remote": "/one", "mode": "rw"}]
    two = [{"local": str(second), "remote": "/two", "mode": "rw"}]
    with headless(settings, komga=_probe(fake)) as env:
        env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY, path_map=one)
        assert _audit(env.engine, "config.path_map") == []  # a creation is the line of its door
        env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY, path_map=one)
        assert _audit(env.engine, "config.path_map") == []  # nothing changed, nothing said
        env.admin.target_add_komga(name="Komga", url=BASE_URL, api_key=API_KEY, path_map=two)
        env.admin.target_add_kavita(
            name="K", url="http://kavita.internal:5000", username="u", password="p" * 12,
            accept_admin_risk=True, path_map=one,
        )  # fmt: skip
        env.admin.target_add_kavita(
            name="K", url="http://kavita.internal:5000", username="u", password="p" * 12,
            accept_admin_risk=True, path_map=two,
        )  # fmt: skip
        assert [r.target for r in _audit(env.engine, "config.path_map")] == [
            "Komga uid=" + str(os.getuid()),
            "K uid=" + str(os.getuid()),
        ]


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_the_client_of_a_target_is_built_where_no_loop_runs(tmp_path: Path) -> None:
    """Building the fetcher of a target runs a loop of its own (`Network.integration_fetcher`):
    the progress pass must not build the client inside the loop it reads Kavita on, or the job
    dies on `asyncio.run() cannot be called from a running event loop` (found by the e2e)."""
    import asyncio

    from tests.sync_env import sync_env

    with sync_env(tmp_path) as env:
        admin = env.remote._admin_client

        def client_of(target: str):  # type: ignore[no-untyped-def]
            asyncio.run(asyncio.sleep(0))  # what the real fetcher does when it is built
            return admin(target)

        env.remote._admin_client = client_of
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 7)
        assert env.sync(reader).code == "ok"
        assert [p.locator["page"] for p in env.positions(reader, source="server")] == [7]
