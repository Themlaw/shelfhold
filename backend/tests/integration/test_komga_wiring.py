"""EF-EXP-020, ENF-SEC-013 (M4 L08a): the Komga client of the application is built with its sealed
key and its write guard, and a shelf in pause is never scanned through it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.export.targets.komga import KomgaDestination, KomgaKey, store_komga_key
from shelfhold.export.targets.komga.wiring import open_client
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.runtime_jobs import FirstPartyDestinations
from tests.export_env import export_env
from tests.fakes.komga import API_KEY, BASE_URL, FakeKomga, build_fetcher
from tests.fakes_clock import ManualClock
from tests.fan_out_env import add_target, bind, service, tree_of
from tests.komga_env import run
from tests.l41_env import instance

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013")
def test_komga_client_is_opened_with_its_sealed_key_and_the_write_guard() -> None:
    with export_env() as env:
        target = add_target(env, kind="komga", partitioning="none", name="Komga")
        # a Komga binding never owns a tree (J-SIMP-4): the shelf's own binding is the tree
        service(env).list_bindings(env.shelf_id)
        tree_id = tree_of(env, env.destination_id)
        assert tree_id is not None
        bind(env, "Kavita", target_id=target, attach_to_tree=tree_id)
        vault = SecretVault(Keyring.generate(NOW))
        with write_transaction(env.engine) as connection:
            registry.sync_registry(connection, vault.keyring)
            store_komga_key(vault, connection, target, KomgaKey(API_KEY))
        fake = FakeKomga()
        client = open_client(
            env.engine, vault, build_fetcher(fake), target_id=target, base_url=BASE_URL,
            clock=ManualClock(NOW),
        )  # fmt: skip
        assert run(client.list_libraries())
        run(client.request_scan_for_path("/manga/Synthetic Series A"))
        assert run(client.flush_scans(now=True)) == ["0SYNTHMANGA01"]
        assert {r.headers["x-api-key"] for r in fake.requests} == {API_KEY}

        with write_transaction(env.engine) as connection:  # a shelf of the target is paused
            connection.execute(sa.update(export.shelf).values(paused_reason="child_exposure"))
        run(client.request_scan_for_path("/manga/Synthetic Series A"))
        with pytest.raises(DestinationPausedError):
            run(client.flush_scans(now=True))
        assert fake.scans == ["0SYNTHMANGA01"]  # the one before the pause, none after
        assert run(client.list_libraries())  # reading stays possible


@pytest.mark.req("EF-EXP-020")
def test_the_core_reads_the_capabilities_of_a_komga_target_and_never_runs_it_as_kavita(
    settings: Settings, tmp_path: Path
) -> None:
    """A Komga is declared to the pass by its adapter, and the Kavita client refuses it: its key
    is sealed under another name, and its address would otherwise be called with Kavita routes."""
    with instance(settings, tmp_path) as inst:
        target = new_ulid()
        with write_transaction(inst.engine) as connection:
            connection.execute(
                sa.insert(export.export_target).values(
                    id=target, kind="komga", name="Salon", config={"url": BASE_URL}
                )
            )
        declared = FirstPartyDestinations(inst.engine).capabilities(target)
        assert declared is KomgaDestination.capabilities
        with pytest.raises(KeyError):
            inst.comp.kavita_client(target)
