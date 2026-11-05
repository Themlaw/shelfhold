"""L18: the always-on follow cycle, download tickets, failure classes (EF-SRC-009, 025, 027, 028).

Everything runs against the simulated Suwayomi (`tests/fakes/suwayomi`) and a real SQLite and
/library: the cycle's own behaviour is what is under test, not a mock of it.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from shelfhold.sdk.connected import ConnectedCapability, ServiceHealth, Ticket, TicketState
from shelfhold.sdk.fetcher import Response
from shelfhold.sdk.refs import FileAsset, ImageRequest, RangedPayload, SeriesRef, UnitInfo, UnitRef
from shelfhold.sdk.source import TemporarilyUnavailable
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
    """Requests to one upstream source are spaced, with a jitter; no refresh is ever sent."""
    with follow_bench() as bench:
        bench.run()
        for number in (3, 4, 5):
            bench.add_chapter(number)
        bench.run()
        interval = 1 / bench.service.manifest.rate_limit.requests_per_second
        assert len(bench.sleeps) == 2
        assert all(interval <= s <= interval * 1.5 for s in bench.sleeps)
        assert len({round(s, 6) for s in bench.sleeps}) == 2  # a jitter, not a fixed step
        enqueues = [r for r in bench.server.requests if "enqueueChapterDownload" in r.fields]
        assert len(enqueues) == 3
        assert not any(REFRESH_FIELDS & r.fields for r in bench.server.requests)


@pytest.mark.req("EF-SRC-009")
def test_the_pace_survives_a_cycle_and_a_waiting_source_holds_back_no_other() -> None:
    with follow_bench() as bench:
        bench.add_binding("suwayomi:7002", "/series-beta")
        bench.run()
        for number in (3, 4, 5):
            bench.add_chapter(number)
        bench.server.state["mangas"][1]["chapters"].append(
            new_chapter(6001, 2, "/series-beta/ch-002")
        )
        start = bench.clock.now()
        bench.run()
        by_source = {
            row["source_key"]: row["updated_at"]
            for row in bench.rows(
                "SELECT si.source_key, max(r.updated_at) AS updated_at FROM download_request r "
                "JOIN source_binding b ON b.id = r.source_binding_id "
                "JOIN source_item si ON si.id = b.source_item_id GROUP BY si.source_key"
            )
        }
        assert len(bench.sleeps) == 2  # only Alpha's own source waited, for its own chapters
        assert str(by_source["suwayomi:7002"]).startswith("2026-11-05 09:00:00")  # Beta did not
        assert bench.clock.now() > start

        bench.add_chapter(6)  # a new process: the pace was kept in the database
        before = len(bench.sleeps)
        bench.new_cycle()
        assert asyncio.run(bench.new_cycle().run()).requested == 1
        assert len(bench.sleeps) == before + 1


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
        copies_before = bench.rows("SELECT count(*) AS n FROM replica")[0]["n"]

        bench.alpha()["inLibrary"] = False  # the user removes the series from Suwayomi
        assert bench.run().removed == 0 and bench.binding()["active"]  # one look is not enough
        report = bench.run()
        assert report.removed == 1
        binding = bench.binding()
        assert not binding["active"] and binding["disabled_reason"] == "removed_upstream"
        assert bench.rows("SELECT 1 FROM series_follow")  # never a cascade
        assert bench.library_files() == files  # the files stay
        assert bench.rows("SELECT count(*) AS n FROM replica")[0]["n"] == copies_before
        assert [e["edition_id"] for e in bench.events("series.removed_upstream")] == [
            bench.edition_id
        ]
        assert any(e.type == "series.removed_upstream" for e in bench.delivered)

        bench.add_chapter(4)
        again = bench.run()  # nothing is followed any more, nothing is announced twice
        assert again.discovered == 0 and len(bench.events("series.removed_upstream")) == 1

        bench.alpha()["inLibrary"] = True  # it comes back: the follow resumes
        assert bench.run().removed == 0
        assert bench.binding()["active"]


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
def test_truncated_archive_is_retried_without_counting_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with follow_bench() as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()
        real = bench.server._download

        def cut_short(path: str) -> Response:  # a download still running on the Suwayomi side
            reply = real(path)
            return Response(status=reply.status, headers=reply.headers, body=reply.body[:40])

        monkeypatch.setattr(bench.server, "_download", cut_short)
        report = bench.run()
        assert report.fetched == 0
        row = bench.rows("SELECT state, attempts FROM download_request")[0]
        assert row["state"] == "requested" and row["attempts"] == 1
        assert bench.library_files() == []
        assert bench.rows("SELECT * FROM source_failure") == []

        monkeypatch.setattr(bench.server, "_download", real)  # Suwayomi finished
        assert bench.run().fetched == 1


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
        assert bench.binding()["active"]  # nothing deactivated


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

    minimal = MinimalService("suwayomi:7001", "/series-alpha", b"", BASE_URL)
    with follow_bench(service=minimal) as bench:  # the cycle only knows sdk.connected
        assert bench.run().requested == 0  # baseline
        minimal.chapters.append("c2")
        report = bench.run()
        assert report.discovered == 1 and report.requested == 1 and report.fetched == 1
        assert [p.suffix for p in bench.library_files()] == [".cbz"]


@pytest.mark.req("EF-SRC-009")
def test_one_follow_job_per_service_and_one_open_ticket_per_variant(tmp_path: Path) -> None:
    from shelfhold.acquisition.public import (
        FOLLOW_JOB_KIND,
        DownloadTickets,
        follow_dedupe_key,
        submit_follow,
    )
    from shelfhold.jobs.public import JobPools

    assert follow_dedupe_key("suwayomi") == "follow.cycle:suwayomi"
    with follow_bench() as bench:
        pools = JobPools(tmp_path, engine=bench.lib.engine, clock=bench.clock, seed="s")
        pools.register(FOLLOW_JOB_KIND, pool="io", handler=lambda context: None)
        first, second = submit_follow(pools, "suwayomi"), submit_follow(pools, "suwayomi")
        assert first.created and not second.created and first.job_id == second.job_id

        bench.run()
        bench.add_chapter(3)
        bench.run()
        row = bench.rows("SELECT variant_id, source_binding_id FROM download_request")[0]
        unit = bench.rows("SELECT unit_id FROM unit_variant WHERE id = :v", v=row["variant_id"])
        with bench.lib.engine.begin() as connection:  # a racing writer opens it again: refused
            again = DownloadTickets.open(
                connection, binding_id=row["source_binding_id"],
                variant_id=row["variant_id"], unit_id=unit[0]["unit_id"], now=bench.clock.now(),
            )  # fmt: skip
        assert again is False
        assert len(bench.rows("SELECT id FROM download_request")) == 1


@pytest.mark.req("EF-SRC-009")
def test_pause_is_announced_once_even_if_two_writers_race() -> None:
    from shelfhold.acquisition.public import FailureLedger, Pause

    with follow_bench() as bench:
        ledger = FailureLedger("suwayomi")
        pause = Pause("auth", "source.auth_expired", "auth_expired")
        with bench.lib.engine.begin() as connection:
            assert ledger.pause(connection, pause, bench.clock.now()) is True
            assert ledger.pause(connection, pause, bench.clock.now()) is False
            ledger.record_download(connection, "suwayomi:7001", ok=True, now=bench.clock.now())
            assert ledger.paused(connection) is not None  # the counters did not erase the pause
            assert ledger.resume(connection) is True and ledger.resume(connection) is False


@pytest.mark.req("EF-SRC-027")
def test_a_series_back_after_one_cycle_of_absence_is_never_deactivated() -> None:
    with follow_bench() as bench:
        bench.run()
        for _ in range(3):
            bench.alpha()["inLibrary"] = False
            assert bench.run().removed == 0  # absent once...
            bench.alpha()["inLibrary"] = True
            assert bench.run().removed == 0  # ...back: the count starts again
        assert bench.binding()["active"] and bench.events("series.removed_upstream") == []


@pytest.mark.req("EF-SRC-027")
def test_an_emptied_library_deactivates_nothing_and_alerts_once() -> None:
    with follow_bench() as bench:
        bench.run()
        for manga in bench.server.state["mangas"]:
            manga["inLibrary"] = False  # reinstalled or not yet loaded: the whole library is gone
        for _ in range(4):
            assert bench.run().removed == 0
        assert bench.binding()["active"]
        assert bench.events("series.removed_upstream") == []
        assert len(bench.events("source.instance_changed")) == 1
        assert any(e.type == "source.instance_changed" for e in bench.delivered)

        for manga in bench.server.state["mangas"]:
            manga["inLibrary"] = True  # the tool is back: following goes on, a later alert may come
        assert bench.run().removed == 0 and bench.binding()["active"]


@pytest.mark.req("EF-SRC-009")
def test_one_failing_series_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    with follow_bench() as bench:
        beta = bench.add_binding("suwayomi:7002", "/series-beta")
        original = bench.service.list_units

        async def flaky(f: Any, ref: SeriesRef) -> Any:
            if ref.remote_id == "/series-alpha":
                raise TemporarilyUnavailable("asleep")
            return await original(f, ref)

        monkeypatch.setattr(bench.service, "list_units", flaky)
        report = bench.run()
        assert report.paused is None and report.discovered == 1  # Beta's chapter was read
        assert bench.rows("SELECT count(*) AS n FROM unit_variant WHERE source_binding_id = :b",
                          b=beta)[0]["n"] == 1  # fmt: skip
        monkeypatch.setattr(bench.service, "list_units", original)
        assert bench.run().discovered == 2  # Alpha catches up on the next cycle


@pytest.mark.req("EF-SRC-009")
def test_a_variant_never_exists_without_its_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    from shelfhold.acquisition.public import DownloadTickets

    with follow_bench() as bench:
        bench.run()
        bench.add_chapter(3)
        real = DownloadTickets.open

        def crash(*args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("killed between the ingest and the ticket")

        monkeypatch.setattr(DownloadTickets, "open", staticmethod(crash))
        bench.run()  # the chapter is neither recorded nor lost
        assert [u["number_raw"] for u in _units(bench)] == ["1", "2"]
        monkeypatch.setattr(DownloadTickets, "open", staticmethod(real))
        assert bench.run().requested == 1
        assert len(bench.rows("SELECT id FROM download_request")) == 1


def _stuck(bench: Bench, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    async def poll(f: Any, t: Any) -> str:
        return state

    monkeypatch.setattr(bench.service, "poll", poll)


@pytest.mark.req("EF-SRC-026", "EF-SRC-028")
def test_a_5xx_while_receiving_spends_no_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    with follow_bench() as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()
        real = bench.server._download

        def unavailable(path: str) -> Response:
            return Response(status=503, headers={}, body=b"")

        monkeypatch.setattr(bench.server, "_download", unavailable)
        for _ in range(12):  # more than any budget of attempts
            assert bench.run().fetched == 0
        row = bench.rows("SELECT state, attempts FROM download_request")[0]
        assert row["state"] in ("requested", "downloaded") and row["attempts"] == 0
        assert bench.rows("SELECT * FROM source_failure") == []
        monkeypatch.setattr(bench.server, "_download", real)
        assert bench.run().fetched == 1


@pytest.mark.req("EF-SRC-026")
def test_a_chapter_still_not_downloaded_after_six_hours_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with follow_bench() as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()
        _stuck(bench, monkeypatch, "downloading")
        bench.clock.advance(hours=5, minutes=50)
        bench.run()
        assert bench.rows("SELECT attempts FROM download_request")[0]["attempts"] == 0
        assert bench.events("source.downloads_dropped") == []
        bench.clock.advance(minutes=20)  # past N = 6 h
        bench.run()
        assert bench.rows("SELECT attempts FROM download_request")[0]["attempts"] == 1
        assert [e["count"] for e in bench.events("source.downloads_dropped")] == [1]
        assert any(e.type == "source.downloads_dropped" for e in bench.delivered)


@pytest.mark.req("EF-SRC-026")
def test_given_up_tickets_are_announced_and_reopened_when_the_source_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with follow_bench(max_attempts=2) as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()
        _stuck(bench, monkeypatch, "failed")  # the server keeps dropping the chapter
        for _ in range(4):
            bench.run()
        row = bench.rows("SELECT state, attempts FROM download_request")[0]
        assert row["state"] == "abandoned" and row["attempts"] == 2
        assert len(bench.events("source.downloads_dropped")) == 1
        assert bench.rows("SELECT * FROM source_failure") == []

        monkeypatch.undo()  # the source is back; the admin resumes following
        bench.cycle.resume()
        assert bench.rows("SELECT state, attempts FROM download_request")[0] == {
            "state": "requested",
            "attempts": 0,
        }
        bench.run()
        assert (
            bench.run().fetched
            + bench.rows("SELECT count(*) AS n FROM download_request WHERE state = 'fetched'")[0][
                "n"
            ]
            >= 1
        )
