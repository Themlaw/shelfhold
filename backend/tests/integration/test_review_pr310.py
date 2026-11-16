"""#310 (M4 L07, relecture adverse intégrité) : PoC rouges en `xfail(strict=True)`.

Chacun échoue aujourd'hui pour la raison qu'il décrit ; `strict` signalera le jour où il passe.
Détail et remèdes : section « #310 : contradicteur » de `docs/decisions/M4.md`.
"""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import RootGuard
from shelfhold.export.public import RootOverlapsLocalSourceError
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition, export
from tests.byte_source_env import unit_with_item
from tests.export_env import export_env
from tests.fakes.suwayomi import FakeSuwayomi
from tests.follow_env import follow_bench
from tests.integration.test_fan_out_routes import World, world  # noqa: F401
from tests.integration.test_native_folder_pass import native, run, tree
from tests.integration.test_native_folder_root_guard import _bind, _native_target, _suwayomi_source

# ruff: noqa: F811  (the `world` fixture of L06a is imported, then asked for by name)

NATIVE = "/api/v1/targets/native-folder"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """The same library as the routes of L07: a writable root must be under it."""
    return {
        "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
        "SHELFHOLD_DISK_PAUSE_GIB": "0",
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
        "SHELFHOLD_LIBRARY_DIR": str(tmp_path / "library"),
    }


# -- M1 : un nouveau path_map pose la racine native sur la source locale, sans garde -------------


@pytest.mark.req("EF-EXP-010")
@pytest.mark.xfail(
    strict=True,
    reason="#310 M1: `config set path_map.<cible>` (PATCH /config) ne passe pas par RootGuard",
)
def test_a_new_path_map_cannot_move_a_native_root_onto_the_local_source(world: World) -> None:
    free = world.library.parent / "native"
    free.mkdir(exist_ok=True)
    local = world.library.parent / "suwayomi-local"
    local.mkdir(exist_ok=True)
    with write_transaction(world.engine) as connection:
        connection.execute(sa.insert(acquisition.source_config).values(plugin_id="suwayomi"))
        RootGuard().record(connection, "suwayomi", str(local))
    body = {"name": "Mihon", "path_map": [{"local": str(free), "remote": "/n", "mode": "rw"}]}
    created = world.api.post(NATIVE, json=body)
    assert created.status_code == 201, created.text

    moved = [{"local": str(local), "remote": "/n", "mode": "rw"}]
    patched = world.api.client.patch(
        "/api/v1/config",
        json={"values": {"path_map.Mihon": json.dumps(moved)}},
        headers={"X-CSRF-Token": world.api.csrf(world.api.client)},
    )
    with world.engine.connect() as connection:
        stored = connection.execute(
            sa.select(export.export_target.c.path_map).where(export.export_target.c.name == "Mihon")
        ).scalar_one()
    assert patched.status_code >= 400, patched.text
    assert stored[0]["local"] == str(free)


# -- M2 : la boucle détectée met le suivi en pause, pas l'écriture du dossier natif ----------------


@pytest.mark.req("EF-EXP-010")
@pytest.mark.xfail(
    strict=True,
    reason="#310 M2: la garde n'est lue qu'à la liaison ; le passage écrit dans la source locale",
)
def test_a_pass_never_writes_into_the_local_source_suwayomi_now_reads() -> None:
    with export_env() as env:
        series = env.new_series("Dr Stone")
        unit_with_item(env, series, "1")
        native(env)  # bound while Suwayomi read another folder (or was not there yet)
        # a later cycle reads that Suwayomi now takes the native root as its local source: it
        # records it, then pauses the follow (`local_source_loop`)
        _suwayomi_source(env, str(env.lib.manga / "local"))
        fan, _source = run(env)
        fan.run(env.shelf_id, "c1")
        assert tree(env) == []  # nothing written where Suwayomi reads


# -- M3 : la source locale que Suwayomi voit par un autre chemin (le `remote` du path_map) -----


@pytest.mark.req("EF-EXP-010")
@pytest.mark.xfail(
    strict=True,
    reason="#310 M3: le chemin rapporté par Suwayomi n'est comparé qu'aux chemins `local`",
)
def test_the_local_source_seen_through_the_remote_side_of_the_path_map_is_refused() -> None:
    with export_env() as env:
        target = _native_target(env)  # path map: local = the library, remote = /manga
        # Suwayomi mounts the same folder at /manga and reads /manga/local as its local source:
        # in this process the path does not exist, so no stat and no string relates them
        _suwayomi_source(env, "/manga/local")
        with pytest.raises(RootOverlapsLocalSourceError):
            _bind(env, "local", target)


# -- M4 : une réponse sans chemin lève la pause de boucle ---------------------------------------


@pytest.mark.req("EF-EXP-010")
@pytest.mark.xfail(
    strict=True,
    reason="#310 M4: un cycle qui ne lit plus de chemin (None) lève la pause `local_source_loop`",
)
def test_a_loop_pause_is_not_lifted_by_a_local_source_that_can_no_longer_be_read() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        bench.cycle = bench.new_cycle(root_guard=RootGuard())
        server.state["local_source_path"] = str(bench.lib.store)
        assert bench.run().paused == "local_source_loop"
        # the setting is unchanged; the answer can no longer be parsed (a relative or an
        # over-long path, a control character, a schema without the field): unknown, not fixed
        server.state["local_source_path"] = "store"
        assert bench.run().paused == "local_source_loop"
