"""EF-EXP-020 (M4 L08a): the Komga client asks for a scan of the library, never of a folder.

Komga has no folder scan: a series is a folder, and the unit it rescans is the library. The client
collects the folders the export touched, maps each to its library, and once the writing has been
quiet asks for one scan per library. After a deletion the library is scanned first and its trash
emptied right after, so that Komga forgets the books the host removed (and only those of that
library). Every call goes through an allow-list of routes written in `docs/destinations`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from shelfhold.export.targets.kavita.errors import TargetForbidden, TargetUnreachable
from shelfhold.export.targets.komga import (
    ALLOWED_ROUTES,
    KomgaClient,
    KomgaDestination,
    KomgaKey,
    KomgaNoLibrary,
    KomgaRouteNotAllowed,
)
from shelfhold.sdk.api_call import ApiCall, ApiCallError
from tests.fakes.komga import API_KEY, BASE_URL, OTHER_KEY, UNKNOWN_KEY, build_fetcher
from tests.fakes_clock import ManualClock
from tests.komga_env import NOW, documented_routes, harness, run

MANGA, COMICS = "0SYNTHMANGA01", "0SYNTHCOMIC02"


@pytest.mark.req("EF-EXP-020")
def test_komga_library_scan_no_per_folder() -> None:
    h = harness()
    folders = [f"/manga/Synthetic Series {n:02d}" for n in range(28)]
    folders += ["/comics/Synthetic Comic A", "/comics/Synthetic Comic B"]

    async def session() -> list[str]:
        for folder in folders:
            await h.client.request_scan_for_path(folder)
        before = await h.client.flush_scans()  # the writing is not quiet yet
        h.clock.advance(seconds=29)
        still = await h.client.flush_scans()
        h.clock.advance(seconds=2)
        sent = await h.client.flush_scans()
        again = await h.client.flush_scans()
        assert before == still == again == []
        return sent

    sent = run(session())
    assert sorted(sent) == sorted([MANGA, COMICS])
    scans = [r for r in h.fake.requests if r.method == "POST"]
    assert sorted(r.path for r in scans if "/scan" in r.path) == sorted(
        [f"/api/v1/libraries/{MANGA}/scan", f"/api/v1/libraries/{COMICS}/scan"]
    )  # 30 folders, two libraries, two scans
    assert all(not r.body and set(r.query) <= {"deep"} for r in scans)  # no folder on the wire
    assert not any("Synthetic Series" in r.raw_url or "Synthetic Comic" in r.raw_url for r in scans)
    assert h.fake.emptied == [] and h.fake.violations == []  # nothing deleted, trash untouched
    assert KomgaDestination().scan_calls(folders) == []  # the destination describes no folder scan


@pytest.mark.req("EF-EXP-020")
def test_komga_scan_unknown_folder_asks_for_nothing() -> None:
    h = harness()
    with pytest.raises(KomgaNoLibrary):
        run(h.client.request_scan_for_path("/elsewhere/Synthetic Series"))
    with pytest.raises(KomgaNoLibrary):  # a folder is inside a root by components, not by prefix
        run(h.client.request_scan_for_path("/manga-extra/Synthetic Series"))
    h.clock.advance(minutes=10)
    assert run(h.client.flush_scans(now=True)) == [] and h.fake.scans == []


@pytest.mark.req("EF-EXP-020")
def test_komga_empty_trash_follows_the_scan_of_a_library_that_lost_a_book() -> None:
    h = harness()

    async def session() -> None:
        await h.client.request_scan_for_path("/manga/Synthetic Series 01")
        await h.client.request_scan_for_path("/comics/Synthetic Comic A")
        h.client.note_deletion(MANGA)
        await h.client.flush_scans(now=True)

    run(session())
    assert h.fake.order.index(("scan", MANGA)) < h.fake.order.index(("empty-trash", MANGA))
    assert h.fake.emptied == [MANGA]  # the other library lost nothing: its trash is left alone
    run(h.client.flush_scans(now=True))
    assert h.fake.emptied == [MANGA]  # the deletion was noted once, the trash emptied once


@pytest.mark.req("EF-EXP-020")
def test_komga_client_calls_only_allowlisted_routes() -> None:
    h = harness()

    async def session() -> None:
        await h.client.probe()
        await h.client.list_libraries()
        await h.client.request_scan_for_path("/manga/Synthetic Series 01")
        h.client.note_deletion(MANGA)
        await h.client.flush_scans(now=True)

    run(session())
    sent_before = len(h.fake.requests)
    assert h.fake.violations == []
    seen = {(r.method, r.path.replace(MANGA, "{library_id}")) for r in h.fake.requests}
    assert seen <= {(r.method, r.path) for r in ALLOWED_ROUTES}
    documented = documented_routes()
    assert documented == {(r.method, r.path) for r in ALLOWED_ROUTES}  # one list, two writings
    attempts = [
        ("POST", "/api/v1/libraries/0SYNTHMANGA01/scan-folder"),
        ("POST", f"/api/v1/libraries/{MANGA}/scan/Synthetic Series 01"),
        ("POST", "/api/v1/libraries/../users/scan"),
        ("DELETE", f"/api/v1/libraries/{MANGA}"),
        ("GET", "/api/v1/users"),
        ("POST", "/api/v1/libraries/%2e%2e/scan"),
        ("POST", "/api/v1/libraries//scan"),
    ]
    for method, path in attempts:
        with pytest.raises((KomgaRouteNotAllowed, ApiCallError)):  # the call, or the route, refuses
            run(h.client.execute(ApiCall(method=method, path=path, auth="target_admin")))  # type: ignore[arg-type]
    assert h.fake.violations == []
    assert len(h.fake.requests) == sent_before  # none of the attempts left the host


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013")
def test_komga_client_refuses_a_body_a_credential_and_a_scan_while_paused() -> None:
    h = harness()
    call = ApiCall(
        method="POST", path=f"/api/v1/libraries/{MANGA}/scan", json_body={"x": 1},
        auth="target_admin",
    )  # fmt: skip
    from shelfhold.export.targets.kavita.errors import ProtocolError

    with pytest.raises(ProtocolError):
        run(h.client.execute(call))
    queried = ApiCall(
        method="POST", path=f"/api/v1/libraries/{MANGA}/scan", query={"apiKey": "x"},
        auth="target_admin",
    )  # fmt: skip
    with pytest.raises(ProtocolError):
        run(h.client.execute(queried))
    assert h.fake.requests == []  # refused before anything was built

    from shelfhold.export.application.target_state import DestinationPausedError

    def paused() -> None:
        raise DestinationPausedError("target.forbidden")

    h.client.set_write_guard(paused)
    run(h.client.request_scan_for_path("/manga/Synthetic Series 01"))
    with pytest.raises(DestinationPausedError):
        run(h.client.flush_scans(now=True))
    assert h.fake.scans == []  # a paused destination is not scanned (reads stay possible)
    assert run(h.client.list_libraries())


@pytest.mark.req("EF-EXP-020", "ENF-SEC-013")
def test_komga_401_and_403_pause_without_echoing_the_key() -> None:
    revoked = harness(key=UNKNOWN_KEY)
    with pytest.raises(TargetForbidden) as caught:
        run(revoked.client.list_libraries())
    assert UNKNOWN_KEY not in str(caught.value) + repr(caught.value)
    reader = harness(key=OTHER_KEY)  # a key of an account that is not an administrator
    run(reader.client.list_libraries())  # reading its libraries is allowed
    with pytest.raises(TargetForbidden):
        run(reader.client.probe())  # `/actuator/info` asks for an administrator
    assert run(reader.client.flush_scans(now=True)) == []
    unreachable = harness(asleep=True)
    with pytest.raises(TargetUnreachable):
        run(unreachable.client.list_libraries())


@pytest.mark.req("EF-EXP-020")
def test_komga_probe_reads_the_version_and_the_debounce_is_the_one_of_the_core() -> None:
    h = harness(version="1.22.1")
    assert run(h.client.probe()) == "1.22.1"
    quiet = KomgaClient(
        build_fetcher(h.fake), BASE_URL, KomgaKey(API_KEY), clock=ManualClock(NOW),
        scan_quiet=timedelta(seconds=5),
    )  # fmt: skip
    run(quiet.request_scan_for_path("/manga/Synthetic Series 01"))
    assert run(quiet.flush_scans()) == []
