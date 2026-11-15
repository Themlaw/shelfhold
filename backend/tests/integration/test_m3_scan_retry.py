"""A Kavita scan that could not be asked is written down and asked again, never swallowed
(EF-BIB-021, M3 closure review, major 3): the lost scans of a withdrawal or a detachment, and
the retry the export run makes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shelfhold.export.application.pending_scans import (
    load_pending_scans,
    remember_scans,
)
from shelfhold.export.targets.kavita.errors import TargetUnreachable
from shelfhold.infrastructure.db import write_transaction
from shelfhold.ops.library import LibraryOps, ScanUnavailableError
from tests.export_env import export_env

pytestmark = pytest.mark.req("EF-BIB-021")


class _Client:
    def __init__(self, up: bool) -> None:
        self.up = up
        self.scanned: list[str] = []

    async def scan_folder(self, path: str) -> None:
        if not self.up:
            raise TargetUnreachable("Kavita is down")
        self.scanned.append(path)


def _ops(env: object, client: _Client) -> LibraryOps:
    runtime = SimpleNamespace(engine=env.engine, kavita_client=lambda _target: client)  # type: ignore[attr-defined]
    return LibraryOps(runtime)  # type: ignore[arg-type]


def test_an_unreachable_kavita_is_not_swallowed_and_is_asked_again_later() -> None:
    with export_env() as env:
        client = _Client(up=False)
        ops = _ops(env, client)
        with pytest.raises(ScanUnavailableError):
            ops._scan_everywhere(["/data/Kavita/Dr Stone"])
        remember_scans(env.engine, ["/data/Kavita/Dr Stone", "/data/Kavita/Dr Stone"])
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["/data/Kavita/Dr Stone"]  # once, not twice

        assert ops.retry_pending_scans() == {"removed": 0, "waiting": 1}  # still down: kept
        client.up = True
        assert ops.retry_pending_scans() == {"removed": 0, "waiting": 0}
        assert client.scanned == ["/data/Kavita/Dr Stone"]
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == []  # asked, so forgotten
        assert ops.retry_pending_scans() == {"removed": 0, "waiting": 0}  # nothing waits


def test_settling_touches_only_its_own_entries_and_reads_the_old_list_shape() -> None:
    import sqlalchemy as sa

    from shelfhold.export.application.pending_scans import PENDING_SCANS_KEY, settle_entries
    from shelfhold.infrastructure.schema import platform

    with export_env() as env:
        with write_transaction(env.engine) as connection:  # the shape before the counters
            connection.execute(
                sa.insert(platform.app_setting).values(key=PENDING_SCANS_KEY, value='["a", "b"]')
            )
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["a", "b"]
        remember_scans(env.engine, ["c"])
        assert settle_entries(env.engine, PENDING_SCANS_KEY, done=["a"], refused=["zz"]) == []
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["b", "c"]


def test_withdraw_apply_keeps_the_scan_when_kavita_is_down_and_the_retry_asks_it() -> None:
    """The files left the library of the children but Kavita could not be asked: the result says
    the scan is pending, the folder to scan is written down, the next export run asks for it."""
    from contextlib import contextmanager
    from typing import Any

    import shelfhold.ops.library as library
    from shelfhold.identity.public import Credential, Principal

    class Service:
        def __init__(self, *_a: Any, request_scan: Any, **_k: Any) -> None:
            self._scan = request_scan

        def apply(self, **_k: Any) -> Any:
            self._scan("/data/Kavita/Dr Stone")
            return SimpleNamespace(withdrawn=("e1",), files=2, pending=())

    with export_env() as env:
        client = _Client(up=False)
        write = SimpleNamespace(fsops=None, loaded=SimpleNamespace(locate=None))

        @contextmanager
        def write_context() -> Any:
            yield write

        runtime = SimpleNamespace(
            engine=env.engine, kavita_client=lambda _target: client, write_context=write_context,
            clock=env.clock, settings=SimpleNamespace(store_dir="/store", fresh_auth_s=900),
        )  # fmt: skip
        ops = LibraryOps(runtime)  # type: ignore[arg-type]
        admin = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
        original = library.WithdrawalService
        library.WithdrawalService = Service  # type: ignore[misc,assignment]
        try:
            done = ops.withdraw_apply(if_match="v", principal=admin)
        finally:
            library.WithdrawalService = original  # type: ignore[misc]
        assert done["withdrawn"] == ["e1"] and done["scan_pending"] is True
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["/data/Kavita/Dr Stone"]
        client.up = True
        ops.retry_pending_scans()
        assert client.scanned == ["/data/Kavita/Dr Stone"]
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == []


# -- #263 (relecture adverse): one path never holds the others, nothing is lost, nothing loops --


class _Scripted:
    """A Kavita that answers each path as told: `refuse` (a 400), `outage` (a 503), else fine."""

    def __init__(self, *, refuse: tuple[str, ...] = (), outage: tuple[str, ...] = ()) -> None:
        self.refuse, self.outage = refuse, outage
        self.scanned: list[str] = []

    async def scan_folder(self, path: str) -> None:
        from shelfhold.export.targets.kavita.errors import ProtocolError

        if path in self.refuse:
            raise ProtocolError("scan-folder answered 400", status=400)
        if path in self.outage:
            raise ProtocolError("scan-folder answered 503", status=503)
        self.scanned.append(path)


def _runtime(env: object, client: object, *, bus: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        engine=env.engine,
        kavita_client=lambda _t: client,
        bus=bus,  # type: ignore[attr-defined]
    )


def test_a_refused_path_is_counted_then_given_up_with_an_event_and_never_holds_the_others() -> None:
    import sqlalchemy as sa

    from shelfhold.events.bus import EventBus
    from shelfhold.export.application.pending_scans import MAX_ATTEMPTS
    from shelfhold.infrastructure.schema import platform

    with export_env() as env:
        bad, good = "/data/Kavita/Bad", "/data/Kavita/Good"
        remember_scans(env.engine, [bad, good])
        client = _Scripted(refuse=(bad,))
        ops = LibraryOps(_runtime(env, client, bus=EventBus(env.clock)))  # type: ignore[arg-type]
        for _ in range(MAX_ATTEMPTS - 1):
            ops.retry_pending_scans()
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == [bad]  # the other one went at the first pass
        assert client.scanned == [good]
        assert ops.retry_pending_scans() == {"removed": 0, "waiting": 0}  # the last attempt
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == []  # given up
            told = connection.execute(
                sa.select(platform.event.c.type).where(
                    platform.event.c.type == "kavita.scan_abandoned"
                )
            ).all()
        assert len(told) == 1  # the admin is told, once


def test_an_outage_stops_the_pass_without_counting_an_attempt() -> None:
    with export_env() as env:
        first, second = "/data/Kavita/A", "/data/Kavita/B"
        remember_scans(env.engine, [first, second])
        client = _Scripted(outage=(first,))  # a 5xx is Kavita being down, not a refused path
        ops = LibraryOps(_runtime(env, client))  # type: ignore[arg-type]
        for _ in range(8):  # far more than the attempts allowed: an outage never wears a path out
            assert ops.retry_pending_scans() == {"removed": 0, "waiting": 2}
        assert client.scanned == []
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == [first, second]


def test_withdraw_apply_says_scan_pending_only_when_a_scan_was_not_sent() -> None:
    from contextlib import contextmanager
    from typing import Any

    import shelfhold.ops.library as library
    from shelfhold.identity.public import Credential, Principal

    class Service:
        def __init__(self, *_a: Any, request_scan: Any, **_k: Any) -> None:
            self._scan = request_scan

        def apply(self, **_k: Any) -> Any:
            self._scan("/data/Kavita/Dr Stone")
            return SimpleNamespace(withdrawn=("e1",), files=2, pending=())

    with export_env() as env:
        client = _Scripted()

        @contextmanager
        def write_context() -> Any:
            yield SimpleNamespace(fsops=None, loaded=SimpleNamespace(locate=None))

        runtime = SimpleNamespace(
            engine=env.engine, kavita_client=lambda _t: client, write_context=write_context,
            clock=env.clock, settings=SimpleNamespace(store_dir="/store", fresh_auth_s=900),
        )  # fmt: skip
        admin = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
        original = library.WithdrawalService
        library.WithdrawalService = Service  # type: ignore[misc,assignment]
        try:
            done = LibraryOps(runtime).withdraw_apply(if_match="v", principal=admin)  # type: ignore[arg-type]
        finally:
            library.WithdrawalService = original  # type: ignore[misc]
        assert done["scan_pending"] is False and client.scanned == ["/data/Kavita/Dr Stone"]
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == []


def test_a_scan_the_export_could_not_send_is_written_down() -> None:
    from shelfhold.runtime_jobs import ScanRelay

    class Flush:
        async def flush_scans(self, *_a: object, **_k: object) -> list[str]:
            raise TargetUnreachable("Kavita is down")

    with export_env() as env:
        relay = ScanRelay(env.engine)
        relay.client = Flush()  # type: ignore[assignment]
        relay.asked = ["/data/Kavita/Dr Stone"]
        relay.drain()
        assert relay.asked == []
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["/data/Kavita/Dr Stone"]


def test_the_wizard_probe_scan_that_fails_is_written_down() -> None:
    import asyncio

    from shelfhold.ops.wizard import WizardOps

    with export_env() as env:
        wizard = WizardOps(SimpleNamespace(engine=env.engine))  # type: ignore[arg-type]
        asyncio.run(wizard._forget(_Client(up=False), "/data/Kavita/.probe"))  # type: ignore[arg-type]
        with env.engine.connect() as connection:
            assert load_pending_scans(connection) == ["/data/Kavita/.probe"]


# -- a suggestion is never a condition of what arrived (the three doors) -----------------------


def test_a_failed_suggestion_never_fails_the_acquisition_nor_the_follow_nor_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shelfhold.acquisition.application.acquire as acquire
    from shelfhold.acquisition.application.acquire import NativeAcquirer
    from shelfhold.ops.dedup import suggest_on_arrival
    from shelfhold.ops.tombstone_screens import CatalogScreens

    def broken(*_a: object, **_k: object) -> list[str]:
        raise RuntimeError("boom")

    with export_env() as env:
        # the acquisition: after its own commit, in its own transaction
        monkeypatch.setattr(acquire, "suggest_duplicates", broken)
        NativeAcquirer._suggest_titles(SimpleNamespace(_engine=env.engine), object())  # type: ignore[arg-type]
        # the follow cycle and the import folder both go through `suggest_on_arrival`
        monkeypatch.setattr(CatalogScreens, "open", lambda _self: (_ for _ in ()).throw(OSError()))
        assert suggest_on_arrival(SimpleNamespace(engine=env.engine)) == 0  # type: ignore[arg-type]


def test_a_folder_of_a_root_that_is_not_mounted_stays_written_down() -> None:
    import shutil
    from contextlib import nullcontext

    from shelfhold.export.application.deletion import DeletionService
    from shelfhold.library.public import DeletionLevel, StoreIndex
    from tests.deletion_env import Deletion, spread_unit
    from tests.null_tombstones import NONE_PURGED

    with export_env() as env:
        index = StoreIndex(
            env.lib.engine, env.lib.fsops, str(env.lib.store), NONE_PURGED, hold=nullcontext
        )

        def failing(_path: str) -> None:
            raise RuntimeError("Kavita is down")

        def service(scan: object) -> DeletionService:
            return DeletionService(
                env.lib.engine, env.lib.fsops, clock=env.clock, locate=env.lib.locate,
                request_scan=scan, refresh_index=index.rewrite, fresh_auth_s=900,
            )  # type: ignore[arg-type]  # fmt: skip

        d = Deletion(env, service(failing), index)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        report = d.apply(DeletionLevel.REMOVE_DESTINATIONS, [unit])
        assert report.folders_waiting  # type: ignore[attr-defined]
        shutil.rmtree(env.lib.manga / "Kavita")  # the disk is not mounted: nothing can be listed
        again = service(lambda _p: None)
        removed, kept, waiting = again.retry_pending()
        assert removed == () and waiting == () and kept  # the scan went, the folder is unknown
        assert again.pending_folders()  # so the label stays for when the disk comes back
