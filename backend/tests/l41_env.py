"""A real instance in process for the L41 tests: migrated database, keyring on disk, the headless
admin that configures it, and a `Composition` whose only reachable servers are the simulated
Kavita and Suwayomi (one routing transport; the SSRF guard still runs)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx2
from sqlalchemy import Engine

from shelfhold.composition import Composition
from shelfhold.config import Settings
from shelfhold.export.application.root_bootstrap import RootPolicy
from shelfhold.headless import ExchangedKey, HeadlessAdmin
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher import Network
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.jobs.huey_app import JobContext
from tests.fakes.kavita import API_KEY, FakeKavita
from tests.fakes.lncrawl import FakeLncrawl
from tests.fakes.kavita import BASE_URL as KAVITA_URL
from tests.fakes.kavita.server import LAN_ADDRESS
from tests.fakes.suwayomi import FakeSuwayomi
from tests.fakes.suwayomi.server import BASE_URL as SUWAYOMI_URL
from tests.fakes.suwayomi.server import PASSWORD, USER
from tests.fakes_clock import ManualClock
from tests.fakes_fetcher import FakeResolver
from tests.headless_env import FakeChannels, FakeSuwayomiProbe

KAVITA_PASSWORD = "synthetic"  # noqa: S105


class _Exchange:
    def exchange(
        self, *, url: str, username: str, password: str, key_name: str, expires_days: int
    ) -> ExchangedKey:
        expiry = SystemClock().now() + timedelta(days=expires_days)
        return ExchangedKey(API_KEY, expiry, "0.9.1.4")

    def revoke(self, **_: object) -> bool:
        return True


def context(kind: str) -> JobContext:
    from shelfhold.jobs.ledger import JobLedger

    clock = ManualClock(SystemClock().now())
    return JobContext(
        job_id="job", kind=kind, dedupe_key=kind, scope_key="", limit_key=None, window_end=None,
        cursor=0, clock=clock, ledger=JobLedger(None, clock),  # type: ignore[arg-type]
    )  # fmt: skip


@dataclass
class Instance:
    settings: Settings
    engine: Engine
    admin: HeadlessAdmin
    comp: Composition
    kavita: FakeKavita
    suwayomi: FakeSuwayomi
    library: Path
    network: Network
    lncrawl: FakeLncrawl

    def configure(self, *, suwayomi: bool = False) -> None:
        self.admin.target_add_kavita(
            name="k", url=KAVITA_URL, username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
            path_map=[{"local": str(self.library), "remote": "/manga", "mode": "rw"}],
        )  # fmt: skip
        self.admin.shelf_create(name="Adultes", max_age="18+", target="k", subpath="Kavita")
        if suwayomi:
            self.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=f"{USER}:{PASSWORD}")


@contextmanager
def instance(settings: Settings, tmp: Path, *, linker: object = None) -> Iterator[Instance]:
    migrate(settings)
    settings.store_dir.mkdir(parents=True, exist_ok=True)
    library_root = tmp / "library"
    library = library_root / "manga"
    (library / "Kavita").mkdir(parents=True)
    engine = create_db_engine(settings.database_path)
    keyring = boot_check(settings, announce=False)
    kavita, suwayomi = FakeKavita(), FakeSuwayomi(auth_mode="basic_auth")
    lncrawl = FakeLncrawl()

    async def route(request: httpx2.Request) -> httpx2.Response:
        host = request.headers.get("host", request.url.host)  # the guard connects by address
        if host.startswith("lncrawl.lan"):
            return await lncrawl.transport.handle_async_request(request)
        target = kavita if host.startswith("kavita.lan") else suwayomi
        return await target.transport.handle_async_request(request)

    resolver = FakeResolver(
        {"kavita.lan": [LAN_ADDRESS], "suwayomi.lan": [LAN_ADDRESS], "lncrawl.lan": [LAN_ADDRESS]}
    )
    network = Network(resolver=resolver, transport=httpx2.MockTransport(route))
    admin = HeadlessAdmin(
        engine, SecretVault(keyring), clock=SystemClock(), kavita=_Exchange(),  # type: ignore[arg-type]
        suwayomi=FakeSuwayomiProbe(mode="basic_auth"), channels=FakeChannels(),
        roots=RootPolicy(library_root, settings.data_dir, settings.store_dir), linker=linker,  # type: ignore[arg-type]
    )  # fmt: skip
    comp = Composition(settings, engine, clock=SystemClock(), network=network)
    try:
        yield Instance(settings, engine, admin, comp, kavita, suwayomi, library, network, lncrawl)
    finally:
        engine.dispose()
