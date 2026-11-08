"""M1b L19 (EF-SRC-029, EF-INT-014): follow and unfollow from Shelfhold.

An approved request makes exactly one entry in the library of the connected service, under replays,
double clicks and two workers at once; the pause of the service is read before anything is sent;
unfollowing keeps the series in the service unless asked, and confirmed. The service is the
simulated Suwayomi, which referees every write it receives.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier, Thread
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.failures import FailureLedger, Pause
from shelfhold.acquisition.application.follow_request import (
    CLAIM_TTL,
    FollowConfirmationRequired,
    FollowRefused,
    FollowRequests,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.errors import ServiceInstanceChanged
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import ContentRemoved
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher
from tests.follow_env import Bench, follow_bench

GAMMA = SeriesRef(source_key="suwayomi:7003", remote_id="/series-gamma", title="Series Gamma")
PAUSED = Pause("instance", "source.instance_changed", "source_instance_changed")
MUTATIONS = ("updateManga", "createCategory", "updateCategory", "updateMangaCategories")


def requests_of(bench: Bench) -> FollowRequests:
    link = SimpleNamespace(service=bench.service, fetcher=bench.fetcher)
    return FollowRequests(bench.lib.engine, bench.clock, lambda: link)


def sent(server: FakeSuwayomi, *fields: str) -> int:
    return sum(1 for r in server.requests if r.fields & set(fields))


def library(server: FakeSuwayomi) -> list[str]:
    return sorted(str(m["url"]) for m in server.state["mangas"] if m["inLibrary"])


def rows(bench: Bench) -> list[dict[str, Any]]:
    return bench.rows("SELECT source_key, remote_id, state FROM follow_request")


@pytest.mark.req("EF-SRC-029")
def test_approved_request_creates_exactly_one_suwayomi_entry() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        before = library(server)
        requests = requests_of(bench)

        first = asyncio.run(requests.follow(GAMMA, requester_user_id=None))
        assert first.state == "followed" and first.sent
        for _ in range(3):  # replays
            again = asyncio.run(requests.follow(GAMMA, requester_user_id=None))
            assert again.state == "followed" and not again.sent

        assert library(server) == sorted([*before, "/series-gamma"])  # one entry, in the library
        (category,) = server.categories
        assert category["name"] == "Shelfhold" and category["includeInDownload"] == "EXCLUDE"
        assert server.categories_of("/series-gamma") == [category["id"]]
        assert sent(server, "updateManga") == 1 and sent(server, "createCategory") == 1
        assert rows(bench) == [
            {"source_key": GAMMA.source_key, "remote_id": GAMMA.remote_id, "state": "followed"}
        ]
        assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_double_click_sends_one_follow() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        requests = requests_of(bench)

        async def clicks() -> list[Any]:
            return await asyncio.gather(
                *(requests.follow(GAMMA, requester_user_id=None) for _ in range(4))
            )

        outcomes = asyncio.run(clicks())
        assert sum(1 for o in outcomes if o.sent) == 1  # one went to the service
        assert {o.state for o in outcomes} <= {"followed", "in_progress"}
        assert sent(server, "updateManga") == 1 and len(rows(bench)) == 1
        assert library(server).count("/series-gamma") == 1


@pytest.mark.req("EF-SRC-029")
def test_two_workers_approving_together_make_one_entry() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        workers = 4
        start = Barrier(workers)
        outcomes: list[Any] = []
        errors: list[BaseException] = []

        def worker() -> None:
            # its own service and fetcher, as another process would have
            link = SimpleNamespace(
                service=SuwayomiService(BASE_URL, now=bench.clock.now),
                fetcher=FakeSuwayomiFetcher(server),
            )
            requests = FollowRequests(bench.lib.engine, bench.clock, lambda: link)
            start.wait()
            try:
                outcomes.append(asyncio.run(requests.follow(GAMMA, requester_user_id=None)))
            except BaseException as error:
                errors.append(error)

        threads = [Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == []
        assert sum(1 for o in outcomes if o.sent) == 1
        assert sent(server, "updateManga") == 1
        assert library(server).count("/series-gamma") == 1
        assert len(rows(bench)) == 1 and rows(bench)[0]["state"] == "followed"
        assert server.violations == []


@pytest.mark.req("EF-SRC-029")
def test_one_request_per_series_is_a_constraint_of_the_database() -> None:
    with follow_bench(FakeSuwayomi(auth_mode="none")) as bench:
        values = {"source_key": GAMMA.source_key, "remote_id": GAMMA.remote_id}
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.follow_request).values(id=new_ulid(), **values)
            )
        with pytest.raises(sa.exc.IntegrityError), bench.lib.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.follow_request).values(id=new_ulid(), **values)
            )


@pytest.mark.req("EF-SRC-029")
def test_a_claim_in_flight_sends_nothing_and_a_dead_one_is_taken_over() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.follow_request).values(
                    id=new_ulid(),
                    source_key=GAMMA.source_key,
                    remote_id=GAMMA.remote_id,
                    state="following",
                    claimed_at=bench.clock.now(),
                )
            )
        requests = requests_of(bench)
        waiting = asyncio.run(requests.follow(GAMMA, requester_user_id=None))
        assert waiting.state == "in_progress" and not server.requests

        bench.clock.advance(seconds=CLAIM_TTL.total_seconds() + 1)  # the worker died
        taken = asyncio.run(requests.follow(GAMMA, requester_user_id=None))
        assert taken.state == "followed" and taken.sent
        assert library(server).count("/series-gamma") == 1 and sent(server, "updateManga") == 1


@pytest.mark.req("EF-SRC-029")
def test_a_failed_call_leaves_the_request_to_be_asked_again() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        unknown = SeriesRef(source_key="suwayomi:7003", remote_id="/nowhere", title="x")
        with pytest.raises(ContentRemoved):
            asyncio.run(requests_of(bench).follow(unknown, requester_user_id=None))
        assert rows(bench) == []  # nothing stale is kept for a series that does not exist
        assert sent(server, *MUTATIONS) == 0


@pytest.mark.req("EF-SRC-029", "EF-SRC-036")
def test_follow_outside_the_cycle_reads_the_pause_and_creates_nothing() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        with bench.lib.engine.begin() as connection:
            FailureLedger("suwayomi").pause(connection, PAUSED, bench.clock.now())
        with pytest.raises(FollowRefused) as refused:
            asyncio.run(requests_of(bench).follow(GAMMA, requester_user_id=None))
        assert refused.value.waiting == "source_instance_changed"
        assert refused.value.code == "follow.service_paused"
        assert rows(bench) == [] and server.requests == []  # not even a look at the service
        assert not server.categories and "/series-gamma" not in library(server)
        with pytest.raises(FollowRefused):  # unfollowing writes to the service too
            asyncio.run(requests_of(bench).unfollow(GAMMA))
        assert server.requests == []


@pytest.mark.req("EF-SRC-029", "EF-SRC-036")
def test_follow_reads_the_fingerprint_and_refuses_another_installation() -> None:
    server = FakeSuwayomi(auth_mode="none")
    server.assign("/series-alpha", server.add_category("Shelfhold", include_in_download="EXCLUDE"))
    with follow_bench(server) as bench:
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.source_config).values(
                    plugin_id="suwayomi", health={}, instance_fingerprint="another-installation"
                )
            )
        with pytest.raises(ServiceInstanceChanged):
            asyncio.run(requests_of(bench).follow(GAMMA, requester_user_id=None))
        assert sent(server, *MUTATIONS) == 0 and rows(bench) == []
        assert "/series-gamma" not in library(server)


@pytest.mark.req("EF-SRC-029")
def test_follow_of_a_series_already_in_the_library_is_attached_without_duplicate() -> None:
    server = FakeSuwayomi(auth_mode="none")
    server.assign("/series-alpha", server.add_category("Shelfhold", include_in_download="EXCLUDE"))
    with follow_bench(server) as bench:
        alpha = SeriesRef(source_key="suwayomi:7001", remote_id="/series-alpha", title="Alpha")
        done = asyncio.run(requests_of(bench).follow(alpha, requester_user_id=None))
        assert done.state == "followed"
        assert sent(server, *MUTATIONS) == 0  # attached as it is
        assert library(server).count("/series-alpha") == 1


@dataclass
class Followed:
    bench: Bench
    server: FakeSuwayomi

    @property
    def requests(self) -> FollowRequests:
        return requests_of(self.bench)


def followed(bench: Bench, server: FakeSuwayomi) -> Followed:
    asyncio.run(requests_of(bench).follow(GAMMA, requester_user_id=None))
    return Followed(bench, server)


@pytest.mark.req("EF-SRC-029")
def test_unfollow_keeps_suwayomi_series_unless_explicit() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        held = followed(bench, server)
        done = asyncio.run(held.requests.unfollow(GAMMA))
        assert done.state == "unfollowed"
        assert "/series-gamma" in library(server)  # still in Suwayomi
        assert server.categories_of("/series-gamma") == []  # but no longer held by Shelfhold
        assert rows(bench)[0]["state"] == "unfollowed"
        asyncio.run(held.requests.unfollow(GAMMA))  # idempotent
        assert library(server).count("/series-gamma") == 1


@pytest.mark.req("EF-SRC-029")
def test_unfollow_explicit_removes_from_suwayomi() -> None:
    server = FakeSuwayomi(auth_mode="none")
    with follow_bench(server) as bench:
        held = followed(bench, server)
        sent_before = len(server.requests)
        with pytest.raises(FollowConfirmationRequired) as needed:
            asyncio.run(held.requests.unfollow(GAMMA, remove_upstream=True))
        assert needed.value.code == "follow.confirmation_required"
        assert len(server.requests) == sent_before and "/series-gamma" in library(server)

        asyncio.run(held.requests.unfollow(GAMMA, remove_upstream=True, confirmed=True))
        assert "/series-gamma" not in library(server)
        assert server.violations == []
        # and it can be followed again: the request comes back to `followed`, once
        again = asyncio.run(held.requests.follow(GAMMA, requester_user_id=None))
        assert again.sent and library(server).count("/series-gamma") == 1
        assert len(rows(bench)) == 1


@pytest.mark.req("EF-SRC-029")
def test_claim_ttl_is_minutes() -> None:
    assert timedelta(seconds=30) < CLAIM_TTL <= timedelta(minutes=10)
