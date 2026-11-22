"""L22 (EF-EXP-020, EF-EXP-047, ENF-SEC-016): the Komga scan relay of the export pass, the daily
audit of the accounts, and the fresh proof a family binding asks.

The pass writes a tree; every Komga that indexes it is asked for the scan of the library that
holds the folder the binding indexes (Komga has no scan of a folder), once the writing is done.
The audit of the accounts runs every day over the shelves bound now: it is what keeps the proof of
partitioning fresh (36 h, the value of L08b) — a proof nobody refreshed is no proof, and a shelf
below 18+ is refused with its reason. Synthetic servers only; keys made at run time.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.composition import Composition
from shelfhold.config import Settings
from shelfhold.events.waiting_state import WaitingState
from shelfhold.export.application.komga_audit_state import proved_for
from shelfhold.export.application.root_bootstrap import RootPolicy
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.target_state import load_health
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.linking import ShelfLinker
from shelfhold.export.targets.komga import KomgaKey, store_komga_key
from shelfhold.headless import HeadlessAdmin
from shelfhold.headless.ports import ExchangedKey
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.fetcher import Network
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.jobs.daily_checks import komga_accounts_audit
from shelfhold.ops.events import BusTargetEvents
from tests.fakes.kavita import API_KEY, FakeKavita
from tests.fakes.kavita.server import LAN_ADDRESS
from tests.fakes.komga import API_KEY as KOMGA_KEY
from tests.fakes.komga import BASE_URL as KOMGA_URL
from tests.fakes.komga import FakeKomga, administrator, reader
from tests.fakes.komga.server import LAN_ADDRESS as KOMGA_LAN
from tests.fakes.suwayomi import FakeSuwayomi
from tests.fakes_fetcher import FakeResolver
from tests.family_env import family_env
from tests.headless_env import FakeChannels, FakeSuwayomiProbe
from tests.integration.test_komga_access_audit import LIBRARY, ROOT, admit, bind_indexer, kit
from tests.komga_env import run
from tests.l41_env import KAVITA_PASSWORD, context
from tests.l50_env import Seeded

KAVITA_URL = "http://kavita.lan:25000"


class _Exchange:
    def exchange(
        self, *, url: str, username: str, password: str, key_name: str, expires_days: int
    ) -> ExchangedKey:
        from datetime import UTC, datetime

        expiry = datetime(2027, 1, 1, tzinfo=UTC)
        return ExchangedKey(API_KEY, expiry, "0.9.1.4")

    def revoke(self, **_: object) -> bool:
        return True


@dataclass
class KomgaInstance:
    settings: Settings
    engine: object
    comp: Composition
    komga: FakeKomga
    library: Path
    keyring: Keyring


@contextmanager
def komga_instance(
    settings: Settings, tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[KomgaInstance]:
    """A real instance (database, keyring, roots) whose reachable servers are the simulated
    Kavita and the simulated Komga; the jobs and the pipeline are the production ones."""
    monkeypatch.setattr(rj, "SCAN_QUIET_S", 0)  # the scans leave at once: no 31 s debounce
    migrate(settings)
    settings.store_dir.mkdir(parents=True, exist_ok=True)
    library_root = tmp / "library"
    library = library_root / "manga"
    (library / "Kavita").mkdir(parents=True)
    engine = create_db_engine(settings.database_path)
    keyring = boot_check(settings, announce=False)
    kavita, suwayomi, komga = FakeKavita(), FakeSuwayomi(auth_mode="basic_auth"), FakeKomga()

    async def route(request: httpx2.Request) -> httpx2.Response:
        host = request.headers.get("host", request.url.host)
        if host.startswith("komga.lan"):
            return await komga.transport.handle_async_request(request)
        target = kavita if host.startswith("kavita.lan") else suwayomi
        return await target.transport.handle_async_request(request)

    resolver = FakeResolver(
        {"kavita.lan": [LAN_ADDRESS], "suwayomi.lan": [LAN_ADDRESS], "komga.lan": [KOMGA_LAN]}
    )
    network = Network(resolver=resolver, transport=httpx2.MockTransport(route))
    admin = HeadlessAdmin(
        engine, SecretVault(keyring), clock=SystemClock(), kavita=_Exchange(),  # type: ignore[arg-type]
        suwayomi=FakeSuwayomiProbe(mode="basic_auth"), channels=FakeChannels(),
        roots=RootPolicy(library_root, settings.data_dir, settings.store_dir),
    )  # fmt: skip
    comp = Composition(settings, engine, clock=SystemClock(), network=network)
    try:
        admin.target_add_kavita(
            name="k", url=KAVITA_URL, username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
            path_map=[{"local": str(library), "remote": "/manga", "mode": "rw"}],
        )  # fmt: skip
        admin.shelf_create(name="Adultes", max_age="18+", target="k", subpath="Kavita")
        yield KomgaInstance(settings, engine, comp, komga, library, keyring)
    finally:
        engine.dispose()


def add_komga_target(inst: KomgaInstance) -> str:
    """A Komga target over the same library folder, its key sealed in its row."""
    target = new_ulid()
    vault = SecretVault(inst.keyring)
    with write_transaction(inst.engine) as connection:  # type: ignore[arg-type]
        registry.sync_registry(connection, vault.keyring)
        connection.execute(
            sa.insert(export.export_target).values(
                id=target, kind="komga", name="Index", config={"url": KOMGA_URL},
                path_map=[{"local": str(inst.library), "remote": "/manga", "mode": "rw"}],
            )
        )  # fmt: skip
        store_komga_key(vault, connection, target, KomgaKey(KOMGA_KEY))
    return target


def attach_indexer(inst: KomgaInstance, target: str, destination_id: str, shelf_id: str) -> str:
    """The Komga binding on the tree of the shelf's own binding (J-SIMP-4: it never owns a tree)."""
    with inst.engine.connect() as connection:  # type: ignore[attr-defined]
        tree_id = connection.execute(
            sa.select(export.tree_indexer.c.tree_id).where(
                export.tree_indexer.c.shelf_destination_id == destination_id
            )
        ).scalar_one()
    return ShelfService(inst.engine, clock=inst.comp.clock).bind_destination(  # type: ignore[arg-type]
        shelf_id=shelf_id, target_id=target, transport="tree", plugin_egress="foyer",
        root_subpath="Kavita", attach_to_tree=tree_id,
    )  # fmt: skip


@contextmanager
def seeded_komga(
    settings: Settings, tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[KomgaInstance, Seeded]]:
    """A configured instance, its shelf linked to the simulated Kavita, and the seeding helpers
    of L50 over it: a chapter received, then the export pass of the production job."""
    with komga_instance(settings, tmp, monkeypatch) as inst:
        destination = rj.tree_destinations(inst.comp)[0]  # type: ignore[arg-type]
        with inst.engine.connect() as connection:  # type: ignore[attr-defined]
            shelf_id = connection.execute(
                sa.select(export.shelf_destination.c.shelf_id).where(
                    export.shelf_destination.c.id == destination.id
                )
            ).scalar_one()
        auditor = AccessAuditor(
            inst.engine, inst.comp.clock, BusTargetEvents(inst.engine, inst.comp.bus)
        )  # type: ignore[arg-type]
        linker = ShelfLinker(inst.engine, inst.comp.clock, auditor)  # type: ignore[arg-type]
        run(
            linker.link(
                inst.comp.kavita_client(destination.target_id),  # type: ignore[arg-type]
                destination.id,
                library_name="Adultes",
            )
        )
        yield inst, Seeded(inst, destination.id, shelf_id, tmp)  # type: ignore[arg-type]


# -- the pass relays the targeted scan ----------------------------------------------------------


@pytest.mark.req("EF-EXP-020")
def test_komga_pass_relays_targeted_scan(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The export pass writes the tree of the shelf's own binding; the Komga that indexes it is
    asked for the scan of the library that holds the folder it indexes — the other library of the
    server is not scanned, and nothing is asked when nothing was written."""
    with seeded_komga(settings, tmp_path, monkeypatch) as (inst, seeded):
        target = add_komga_target(inst)
        attach_indexer(inst, target, seeded.destination_id, seeded.shelf_id)
        series = seeded.new_series()
        seeded.add_chapter(series, "1")
        rj.export_handler(inst.comp)(context("export.run"))  # type: ignore[arg-type]
        assert inst.komga.scans == ["0SYNTHMANGA01"]  # the library whose root holds /manga/Kavita
        assert inst.komga.requests_to("empty-trash") == []  # nothing was removed
        assert seeded.exported_files()  # the pass really wrote
        # a pass with nothing to write asks for nothing
        inst.komga.scans.clear()
        rj.export_handler(inst.comp)(context("export.run"))  # type: ignore[arg-type]
        assert inst.komga.scans == []


# -- the daily audit feeds the proof of partitioning ---------------------------------------------


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
def test_komga_daily_audit_feeds_partitioning_proof() -> None:
    """The daily job replays the audit over the shelves bound now: a gap that opens withdraws the
    proof and pauses the shelf, a repaired server proves the partitioning again and lifts the
    pause — only a complete audit does."""
    with family_env(13) as env:
        fake, client, _auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        assert admit(env, _auditor, client, target).proved
        bind_indexer(env, target)
        audit = komga_accounts_audit(env.engine, env.clock, _events, lambda: [(target, client)])
        fake.users[1] = reader("teen", [LIBRARY], age=6)  # lowered in Komga: the gap opens
        audit()
        assert env.rows(export.shelf)[0].paused_reason == "child_exposure"
        with env.engine.connect() as connection:
            assert not proved_for(connection, target, ROOT, 13)
            health = load_health(connection, target)
        assert health.child_exposure and health.waiting_state is WaitingState.CHILD_EXPOSURE
        fake.users[1] = reader("teen", [LIBRARY], age=13)  # repaired
        audit()
        assert env.rows(export.shelf)[0].paused_reason is None
        with env.engine.connect() as connection:
            assert proved_for(connection, target, ROOT, 13)


# -- a family binding asks a fresh proof ---------------------------------------------------------


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
def test_komga_family_binding_refused_without_fresh_audit() -> None:
    """A proof older than 36 h (the value of L08b) is no proof: the binding of a shelf below 18+
    is refused with `audit_stale`, and the daily audit that runs again is what reopens it."""
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        assert admit(env, auditor, client, target).proved
        env.clock.advance(timedelta(hours=37))  # the proof is older than the 36 h of L08b
        from tests.integration.test_komga_access_audit import try_bind

        refused = try_bind(env, target)
        assert refused is not None and "audit_stale" in refused
        komga_accounts_audit(env.engine, env.clock, _events, lambda: [(target, client)])()
        assert try_bind(env, target) is None  # the audit of the day proved the folder again
