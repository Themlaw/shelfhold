"""L18: the always-on follow cycle, download tickets, failure classes (EF-SRC-009, 025, 027, 028).

Everything runs against the simulated Suwayomi (`tests/fakes/suwayomi`) and a real SQLite and
/library: the cycle's own behaviour is what is under test, not a mock of it.
"""

import asyncio
from typing import Any

import pytest

from shelfhold.sdk.connected import ConnectedCapability, ServiceHealth, Ticket, TicketState
from shelfhold.sdk.refs import FileAsset, ImageRequest, RangedPayload, SeriesRef, UnitInfo, UnitRef
from tests.fakes.suwayomi import FakeSuwayomi
from tests.follow_env import Bench, follow_bench, new_chapter

REFRESH_FIELDS = {"fetchChapters", "fetchManga", "fetchSourceManga", "updateLibrary"}


def _units(bench: Bench) -> list[dict[str, Any]]:
    return bench.rows("SELECT id, number_raw FROM unit WHERE edition_id = :e ORDER BY number_sort",
                      e=bench.edition_id)  # fmt: skip


@pytest.mark.req("EF-SRC-009")
def test_new_chapters_detected_from_suwayomi_graphql() -> None:
    with follow_bench() as bench:
        first = bench.run()  # baseline: what is already there is known, nothing is requested
        assert first.discovered == 2 and first.requested == 0
        assert len(_units(bench)) == 2
        assert bench.rows("SELECT * FROM download_request") == []

        bench.add_chapter(3)  # a new chapter appears upstream
        second = bench.run()
        assert second.discovered == 1 and second.requested == 1
        assert [u["number_raw"] for u in _units(bench)] == ["1", "2", "3"]
        tickets = bench.rows("SELECT state, ticket FROM download_request")
        assert [t["state"] for t in tickets] == ["requested"] and tickets[0]["ticket"]
        assert bench.run().discovered == 0  # seen once, never twice
        assert len(bench.rows("SELECT id FROM download_request")) == 1


@pytest.mark.req("EF-SRC-025")
def test_always_on_mode_sends_no_refresh_mutation() -> None:
    with follow_bench() as bench:
        calls: list[str] = []
        original = bench.service.request_refresh

        async def spy(f: Any, refs: list[SeriesRef]) -> None:
            calls.append("request_refresh")
            await original(f, refs)

        bench.service.request_refresh = spy  # type: ignore[method-assign]
        bench.run()
        bench.add_chapter(3)
        bench.run()
        bench.run()
        sent = {name for r in bench.server.requests for name in r.fields}
        assert not sent & REFRESH_FIELDS and calls == []
        assert bench.server.violations == []


@pytest.mark.req("EF-SRC-009")
def test_download_requests_rate_limited_not_upstream_refresh() -> None:
    """Requests to one upstream source are spaced; another source is not held back."""
    with follow_bench() as bench:
        bench.run()
        for number in (3, 4, 5):
            bench.add_chapter(number)
        bench.run()
        interval = 1 / bench.service.manifest.rate_limit.requests_per_second
        assert len(bench.sleeps) == 2 and all(abs(s - interval) < 1e-6 for s in bench.sleeps)
        enqueues = [r for r in bench.server.requests if "enqueueChapterDownload" in r.fields]
        assert len(enqueues) == 3
        assert not any(REFRESH_FIELDS & r.fields for r in bench.server.requests)


@pytest.mark.req("EF-SRC-027")
def test_series_removed_from_suwayomi_stops_follow_keeps_files() -> None:
    with follow_bench() as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()
        for _ in range(3):
            bench.run()  # the chapter finishes downloading and is filed
        files = bench.library_files()
        assert len(files) == 1
        irreplaceable_before = bench.rows("SELECT count(*) AS n FROM replica")[0]["n"]

        bench.alpha()["inLibrary"] = False  # the user removes the series from Suwayomi
        report = bench.run()
        assert report.removed == 1
        binding = bench.rows("SELECT active, disabled_reason FROM source_binding")[0]
        assert not binding["active"] and binding["disabled_reason"] == "removed_upstream"
        assert bench.rows("SELECT 1 FROM series_follow")  # never a cascade
        assert bench.library_files() == files  # the files stay
        assert bench.rows("SELECT count(*) AS n FROM replica")[0]["n"] == irreplaceable_before
        assert [e["edition_id"] for e in bench.events("series.tombstoned_upstream")] == [
            bench.edition_id
        ]
        assert any(e.type == "series.tombstoned_upstream" for e in bench.delivered)

        before = len(bench.server.requests)
        bench.add_chapter(4)
        again = bench.run()  # nothing is followed any more, nothing is announced twice
        assert again.discovered == 0 and len(bench.events("series.tombstoned_upstream")) == 1
        assert bench.server.requests_to("graphql")[before:] is not None

        bench.alpha()["inLibrary"] = True  # it comes back: the follow resumes
        assert bench.run().removed == 0
        assert bench.rows("SELECT active FROM source_binding")[0]["active"]


@pytest.mark.req("EF-SRC-028")
def test_waits_downloaded_state_no_source_failure_counted() -> None:
    with follow_bench() as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()  # requested, Suwayomi is still downloading
        waiting = bench.rows("SELECT state, attempts FROM download_request")[0]
        assert waiting == {"state": "requested", "attempts": 0}
        assert bench.library_files() == [] and not bench.server.requests_to("/download")

        done = bench.run()  # now it reports "downloaded": fetched, audited, filed
        assert done.fetched == 1
        assert bench.rows("SELECT state FROM download_request")[0]["state"] == "fetched"
        assert [p.suffix for p in bench.library_files()] == [".cbz"]
        assert bench.rows("SELECT * FROM source_failure") == []
        assert bench.rows("SELECT consecutive_failures AS n FROM source_config") in (
            [],
            [{"n": 0}],
        )


@pytest.mark.req("EF-SRC-028")
def test_truncated_archive_is_retried_without_counting_a_failure() -> None:
    with follow_bench() as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()
        chapter = bench.alpha()["chapters"][-1]
        good = chapter["cbz"]
        chapter["cbz"] = "truncated.cbz"  # an archive cut short by a download still running
        (bench.server.state and None)
        from tests.fakes.suwayomi.server import SYNTHETIC

        broken = SYNTHETIC / "truncated.cbz"
        broken.write_bytes((SYNTHETIC / good).read_bytes()[:40])
        try:
            report = bench.run()
            assert report.fetched == 0
            row = bench.rows("SELECT state, attempts FROM download_request")[0]
            assert row["state"] == "requested" and row["attempts"] == 1
            assert bench.library_files() == []
            assert bench.rows("SELECT * FROM source_failure") == []
            chapter["cbz"] = good  # Suwayomi finished: the next attempt succeeds
            assert bench.run().fetched == 1
        finally:
            broken.unlink(missing_ok=True)


@pytest.mark.req("EF-SRC-009", "EF-SRC-023")
def test_suwayomi_401_pauses_service_auth_expired_no_failure_counted() -> None:
    server = FakeSuwayomi(auth_mode="basic_auth")  # the fetcher sends no valid credential
    with follow_bench(server, has_credentials=True) as bench:
        report = bench.run()
        assert report.paused == "auth_expired"
        assert bench.rows("SELECT * FROM source_failure") == []
        assert bench.rows("SELECT consecutive_failures AS n FROM source_config") == [{"n": 0}]
        assert [e["state"] for e in bench.events("source.auth_expired")] == ["auth_expired"]
        assert any(e.type == "source.auth_expired" for e in bench.delivered)

        sent = len(server.requests)
        again = bench.run()  # paused: no request, no second notification
        assert again.paused == "auth_expired" and len(server.requests) == sent
        assert len(bench.events("source.auth_expired")) == 1

        bench.cycle.resume()  # the admin renewed the secret
        server.auth_mode = "none"
        resumed = bench.run()
        assert resumed.paused is None and resumed.discovered == 2


@pytest.mark.req("EF-SRC-009", "EF-SRC-033")
def test_suwayomi_schema_mismatch_pauses_without_counting() -> None:
    with follow_bench() as bench:
        bench.run()
        # a release changed the shape of what it answers: a node without the expected fields
        bench.server._q_mangas = lambda query, variables: {"nodes": [{"unexpected": 1}]}  # type: ignore[method-assign]
        report = bench.run()
        assert report.paused == "schema"
        assert bench.rows("SELECT * FROM source_failure") == []
        assert len(bench.events("source.broken")) == 1
        assert bench.rows("SELECT active FROM source_binding")[0]["active"]  # nothing deactivated


class MinimalService:
    """The least a connected service owes the follow cycle: nothing here is Suwayomi."""

    connected = ConnectedCapability.CATEGORIES | ConnectedCapability.SERVER_QUEUE
    wake_target = None

    def __init__(self, source_key: str, remote_id: str, cbz: bytes, base_url: str) -> None:
        from shelfhold.sdk.models import RateLimit

        self.manifest = type("M", (), {"key": "suwayomi", "rate_limit": RateLimit(
            requests_per_second=100.0)})()  # fmt: skip
        self.ref = SeriesRef(source_key=source_key, remote_id=remote_id, title="Minimal")
        self.chapters = ["c1"]
        self.base_url = base_url
        self.cbz = cbz

    async def followed_series(self, f: Any) -> list[SeriesRef]:
        return [self.ref]

    async def list_units(self, f: Any, ref: SeriesRef) -> list[UnitInfo]:
        return [
            UnitInfo(remote_unit_id=f"min:{c}", source_order=i, number=str(i + 1))
            for i, c in enumerate(self.chapters)
        ]

    async def request(self, f: Any, target: UnitRef | Any) -> Ticket:
        return Ticket(token=target.unit.remote_unit_id, target_kind="unit")

    async def poll(self, f: Any, t: Ticket) -> TicketState:
        return "ready"

    async def fetch(self, f: Any, t: Ticket) -> FileAsset | RangedPayload:
        return FileAsset(
            request=ImageRequest(url=f"{self.base_url}/api/v1/chapter/5001/download"),
            mime="application/vnd.comicbook+zip",
            filename="1.cbz",
        )

    async def cancel(self, f: Any, t: Ticket) -> None:
        return None

    async def set_follow(self, f: Any, ref: SeriesRef, on: bool) -> None:
        return None

    async def semantic_health(self, f: Any) -> ServiceHealth:
        return ServiceHealth(ok=True)


@pytest.mark.req("EF-SRC-033", "EF-SRC-009")
def test_minimal_connected_service_passes_follow_cycle() -> None:
    from tests.fakes.suwayomi import BASE_URL

    with follow_bench() as bench:
        minimal = MinimalService("suwayomi:7001", "/series-alpha", b"", BASE_URL)
        bench.cycle.use_service(minimal)  # the cycle only knows sdk.connected
        assert bench.run().requested == 0  # baseline
        minimal.chapters.append("c2")
        report = bench.run()
        assert report.discovered == 1 and report.requested == 1 and report.fetched == 1
        assert [p.suffix for p in bench.library_files()] == [".cbz"]
        assert asyncio.run(minimal.poll(bench.fetcher, Ticket(token="x", target_kind="unit")))
        _ = new_chapter
