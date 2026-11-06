"""The production composition: what the earlier lots built, plugged together once.

Every service of the application is built here and nowhere else (the HTTP application, the jobs of
the runtime and the host CLI ask this class): the bus with its notification channels, the
tombstone service (replayed, with the flush gate on `Fsops`), the write context of a job (a
`Fsops` that needs this instance's lease, a lease held for the time of the job and renewed), the
Kavita client of a target (sealed key, write guard, operating events on the bus) and the
Suwayomi service with its fetcher.

The one door to other servers is `Network.fetcher_for(url)`: tests replace it with a fetcher over
a simulated server, nothing else in the code reaches the network.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application.roots import LoadedRoots, load_roots
from shelfhold.export.domain.path_map import PathMap, PathMode
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.wiring import open_client
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.fetcher import GuardedFetcher, Network
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.instance import (
    HEARTBEAT_INTERVAL,
    GenerationCounter,
    WriteSession,
    WriteSessions,
)
from shelfhold.infrastructure.schema import acquisition, export, platform
from shelfhold.infrastructure.secrets import SecretVault, diagnostics
from shelfhold.infrastructure.secrets.errors import SecretsError
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.integrations.apprise import operating_bus
from shelfhold.library.public import TombstoneRoot, TombstoneService
from shelfhold.ops.events import BusTargetEvents
from shelfhold.plugin_host.authflow import AuthInjectingFetcher
from shelfhold.sdk.models import AuthBasic

__all__ = [
    "Composition",
    "Gates",
    "KeyringUnavailableError",
    "Network",
    "NoLibraryError",
    "SessionOpener",
    "SuwayomiLink",
    "WriteContext",
    "operating_bus",
]

log = structlog.get_logger(__name__)

RECEIVED_DIR = ".received"  # under a writable root: what the follow cycle receives, before export


@dataclass(frozen=True)
class SuwayomiLink:
    service: Any  # the `ConnectedService`
    fetcher: Any  # the SDK Fetcher the service uses: the credential is injected by the host
    guarded: GuardedFetcher  # the same transport, for the bounded download of the archives
    url: str


_DEFAULT_NETWORK = Network()  # tests replace it; nothing else reaches another server


def default_network() -> Network:
    return _DEFAULT_NETWORK


@dataclass(frozen=True)
class Gates:
    """What only a running server knows: the disk gate, the stop barrier, the restore freeze."""

    write_gate: Callable[[], None] | None = None
    stop_gate: Callable[[], None] | None = None
    writable: Callable[[], None] | None = None
    lock_path: Path | None = None
    on_behind: Callable[[], None] | None = None


@dataclass
class WriteContext:
    """A job's view of the library: every write goes through `fsops`, under our lease."""

    fsops: Fsops
    loaded: LoadedRoots
    session: WriteSession
    tombstones: TombstoneService
    bus: EventBus

    @property
    def writable_roots(self) -> list[str]:
        return [e.path for e in self.loaded.expected]

    def received_path(self, variant_id: str) -> str:
        """Where the follow cycle files a received archive: the first writable root's inbox."""
        return f"{self.writable_roots[0]}/{RECEIVED_DIR}/{variant_id}.cbz"


class Composition:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        *,
        clock: Clock | None = None,
        network: Network | None = None,
        gates: Gates | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.clock = clock or SystemClock()
        self.network = network or default_network()
        self.gates = gates or Gates()
        self.bus = bus or operating_bus(self.clock)
        self._tombstones: tuple[tuple[tuple[str, str], ...], TombstoneService] | None = None
        self._lock = threading.Lock()
        self._boot_id = uuid.uuid4().hex
        self._held_lock = threading.Lock()
        self._held = 0
        self._lease: tuple[ExitStack, WriteSessions, tuple[str, ...]] | None = None

    # -- secrets -----------------------------------------------------------------------------

    def keyring(self) -> Keyring:
        try:
            return diagnostics.boot_check(self.settings, create=False, announce=False)
        except SecretsError as error:
            raise KeyringUnavailableError("the keyring is not readable") from error

    def vault(self) -> SecretVault:
        return SecretVault(self.keyring())

    # -- the roots ---------------------------------------------------------------------------

    def load_roots(self) -> LoadedRoots | None:
        try:
            with self.engine.connect() as connection:
                paths = [
                    r.path_entry for r in connection.execute(sa.select(platform.physical_root))
                ]
                if not paths or connection.execute(sa.select(platform.instance)).first() is None:
                    return None
                return load_roots(connection, PathMap.same_path(paths))
        except sa.exc.OperationalError:  # not migrated yet
            return None

    def open_fsops(self, loaded: LoadedRoots) -> Fsops:
        return Fsops.open(
            loaded.path_map, loaded.expected, write_gate=self.gates.write_gate,
            stop_gate=self.gates.stop_gate, frozen=loaded.restore_pending,
        )  # fmt: skip

    def tombstones(self, fsops: Fsops, loaded: LoadedRoots) -> TombstoneService:
        """The tombstone service, replayed (a purged file is never acquired again). One per
        process while the roots stay the same; the flush gate is posed on the `fsops` given."""
        signature = tuple(
            sorted((r.root_id, r.path) for r in loaded.roots if r.mode is PathMode.RW)
        )
        with self._lock:
            if self._tombstones is None or self._tombstones[0] != signature:
                service = TombstoneService(self.engine, self.keyring(), fsops, self.bus)
                self._tombstones = (signature, service)
                self._replay(service, fsops, loaded)
            service = self._tombstones[1]
        fsops.enforce_tombstone_flush(
            lambda path: any(
                r.path == path and service.has_pending(r.root_id) for r in loaded.roots
            )
        )
        return service

    @staticmethod
    def _replay(service: TombstoneService, fsops: Fsops, loaded: LoadedRoots) -> None:
        service.replay(
            [
                TombstoneRoot(r.root_id, r.path, mounted=fsops.root_state(r.path) is RootState.OK)
                for r in loaded.roots
                if r.mode is PathMode.RW
            ]
        )

    def read_context(self) -> tuple[Fsops, LoadedRoots, TombstoneService]:
        """Everything that reads the library or the tombstones, and writes nothing."""
        loaded = self.load_roots()
        if loaded is None:
            raise NoLibraryError("no physical root is registered")
        fsops = self.open_fsops(loaded)
        return fsops, loaded, self.tombstones(fsops, loaded)

    # -- the lease --------------------------------------------------------------------------

    def sessions_for(self, fsops: Fsops) -> WriteSessions:
        """The identity of this process on the roots (one `boot_id`), over a given `Fsops`."""
        with self.engine.connect() as connection:
            instance_id = connection.execute(
                sa.select(platform.instance.c.instance_id)
            ).scalar_one()
        return WriteSessions(
            fsops, instance_id=instance_id, clock=self.clock, boot_id=self._boot_id,
            lock_path=self.gates.lock_path, generations=GenerationCounter(self.engine),
            on_behind=self.gates.on_behind,
        )  # fmt: skip

    @contextmanager
    def _hold(
        self, fsops: Fsops, loaded: LoadedRoots
    ) -> Iterator[tuple[WriteSessions, tuple[str, ...]]]:
        """The lease of every mounted writable root, shared by the jobs that run together: taken
        by the first, renewed by a heartbeat, given back by the last. A job that released it
        under another would break that one's writes."""
        with self._held_lock:
            if self._held == 0:
                sessions = self.sessions_for(fsops)
                mounted = tuple(
                    e.path for e in loaded.expected if fsops.root_state(e.path) is RootState.OK
                )
                if not mounted:
                    raise NoLibraryError("no writable root is mounted")
                stack = ExitStack()
                session = stack.enter_context(sessions.open(*mounted))
                beat = _Heartbeat(session)
                beat.start()
                stack.callback(beat.stop)
                self._lease = (stack, sessions, mounted)
            self._held += 1
            _stack, sessions, mounted = cast("Any", self._lease)
        try:
            yield sessions, mounted
        finally:
            with self._held_lock:
                self._held -= 1
                if self._held == 0 and self._lease is not None:
                    stack = self._lease[0]
                    self._lease = None
                    stack.close()

    @contextmanager
    def write_context(self) -> Iterator[WriteContext]:
        """The lease for the time of the block; `Fsops` refuses a write without it (ENF-OPS-015)."""
        fsops, loaded, tombstones = self.read_context()
        with self._hold(fsops, loaded) as (sessions, mounted):
            self.sessions_for(fsops).enforce()
            yield WriteContext(fsops, loaded, WriteSession(sessions, mounted), tombstones, self.bus)

    def session_opener(self, fsops: Fsops, loaded: LoadedRoots) -> SessionOpener:
        """The `sessions=` of the export pipeline: the same shared lease, one hold per apply."""
        self.sessions_for(fsops).enforce()
        return SessionOpener(self, fsops, loaded)

    # -- Kavita ------------------------------------------------------------------------------

    def kavita_client(self, target_id: str) -> KavitaClient:
        with self.engine.connect() as connection:
            url = str(
                connection.execute(
                    sa.select(export.export_target.c.config).where(
                        export.export_target.c.id == target_id
                    )
                ).scalar_one()["url"]
            )
        return open_client(
            self.engine, self.vault(), self.network.integration_fetcher(url), target_id=target_id,
            base_url=url, events=BusTargetEvents(self.engine, self.bus), clock=self.clock,
        )  # fmt: skip

    def kavita_targets(self) -> list[str]:
        """The targets with an address and a sealed key: the ones a client can be built for."""
        table = export.export_target
        with self.engine.connect() as connection:
            rows = connection.execute(sa.select(table.c.id, table.c.config).order_by(table.c.id))
            return [r.id for r in rows if isinstance(r.config, dict) and r.config.get("url")]

    # -- Suwayomi ----------------------------------------------------------------------------

    def suwayomi(self) -> SuwayomiLink | None:
        """The configured Suwayomi (service, authenticated fetcher), or None when there is none."""
        from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService

        with self.engine.connect() as connection:
            settings = connection.execute(
                sa.select(acquisition.source_config.c.settings).where(
                    acquisition.source_config.c.plugin_id == "suwayomi"
                )
            ).scalar_one_or_none()
            url = (settings or {}).get("base_url")
            if not url:
                return None
            secret = self._suwayomi_secret(connection)
        guarded = self.network.integration_fetcher(url)
        fetcher: Any = guarded
        if secret is not None:
            fetcher = AuthInjectingFetcher(cast("Any", guarded), AuthBasic(), secret, origin=url)
        return SuwayomiLink(SuwayomiService(url), fetcher, guarded, url)

    def _suwayomi_secret(self, connection: sa.Connection) -> dict[str, str] | None:
        import json

        try:
            raw = self.vault().open_row(connection, "source_config", "suwayomi")
        except Exception:  # no secret sealed: a server without authentication
            return None
        document = json.loads(raw) if raw else {}
        if document.get("username") and document.get("password"):
            return {"username": document["username"], "password": document["password"]}
        return None

    def run(self, work: Any) -> Any:
        return asyncio.run(work)


class SessionOpener:
    """`WriteSessionOpener` of the export pipeline over the shared lease of the composition."""

    def __init__(self, composition: Composition, fsops: Fsops, loaded: LoadedRoots) -> None:
        self._composition = composition
        self._fsops = fsops
        self._loaded = loaded

    @contextmanager
    def open(self, *paths: str, guard: Callable[[], None] | None = None) -> Iterator[WriteSession]:
        with self._composition._hold(self._fsops, self._loaded) as (sessions, mounted):
            yield WriteSession(sessions, mounted, guard)


class _Heartbeat:
    """Renews the leases of a session while a long job runs (a lease lasts three minutes)."""

    def __init__(self, session: WriteSession) -> None:
        self._session = session
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="lease-heartbeat")

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL.total_seconds() / 2):
            try:
                self._session.heartbeat()
            except Exception as error:  # the next write says the lease is lost
                log.warning("lease.heartbeat_failed", error=type(error).__name__)
                return

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
