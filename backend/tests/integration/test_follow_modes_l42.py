"""M3 L42 (EF-UX-006, EF-EXP-053): what `POST /follows` does with the choice of the add sheet.

The sheet must not promise what the server does not do. A follow made with a mode brings exactly
the Units that mode names (`all`: every one the source lists; `new_only`: those the library does
not hold; `from_number`: from that number on), the Edition is filed on the shelf that was chosen,
and a confirmation that is no longer what the estimate says (the count or the size moved since) is
refused with the fresh estimate. The estimate of `new_only` counts what the library lacks, and a
source that cannot list its Units is an explicit error, never zero.
"""

from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.follow_request import FollowRequests
from shelfhold.acquisition.public import FollowIntent
from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, export
from shelfhold.infrastructure.schema import replica as replica_schema
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.source import SourceError
from tests.auth_env import CSRF_HEADER
from tests.federated_env import FakeSource, api_key_of, hit, instance
from tests.follow_env import Bench, add_adult_shelf, follow_bench
from tests.integration.test_schema_catalog import add_edition, add_unit, add_work

ALPHA = SeriesRef(source_key="suwayomi:7001", remote_id="/series-alpha", title="Series Alpha")
FOLLOWS = "/api/v1/follows"
ESTIMATE = "/api/v1/sources/alpha/series/a1/estimate"


# -- what a follow brings: the bench of the follow cycle, over the simulated Suwayomi -----------


def follow(bench: Bench, intent: FollowIntent) -> None:
    link = SimpleNamespace(service=bench.service, fetcher=bench.fetcher)
    requests = FollowRequests(bench.lib.engine, bench.clock, lambda: link)
    outcome = asyncio.run(requests.follow(ALPHA, requester_user_id=None, intent=intent))
    assert outcome.state == "followed"


def requested_numbers(bench: Bench) -> list[int]:
    """The chapters of Series Alpha a ticket was opened for, whatever happened to them since."""
    rows = bench.rows(
        "SELECT u.number_norm AS n FROM download_request_unit ru "
        "JOIN unit u ON u.id = ru.unit_id "
        "JOIN source_binding b ON b.edition_id = u.edition_id "
        "JOIN source_item i ON i.id = b.source_item_id WHERE i.remote_id = '/series-alpha'"
    )
    return sorted(int(Decimal(r["n"])) for r in rows)


def five_chapters(bench: Bench) -> None:
    for number in (3, 4, 5):  # the simulated series starts with 1 and 2
        bench.add_chapter(number)


def cycles(bench: Bench, count: int = 3) -> None:
    for _ in range(count):
        bench.run()


@pytest.mark.req("EF-UX-006", "EF-EXP-053")
@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (FollowIntent(mode="all"), [1, 2, 3, 4, 5]),
        (FollowIntent(mode="new_only"), [1, 2, 3, 4, 5]),  # nothing is held yet: all is new
        (FollowIntent(mode="from_number", from_number="4"), [4, 5]),
        (FollowIntent(mode="from_number", from_number="2"), [2, 3, 4, 5]),
        (FollowIntent(mode="from_number", from_number="9"), []),  # past the last: nothing
    ],
    ids=["all", "new_only", "from-4", "from-2", "from-past-the-end"],
)
def test_a_follow_brings_exactly_the_units_its_mode_names(
    intent: FollowIntent, expected: list[int]
) -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        five_chapters(bench)
        follow(bench, intent)
        cycles(bench)
        assert requested_numbers(bench) == expected


@pytest.mark.req("EF-UX-006", "EF-EXP-053")
def test_a_series_nobody_chose_for_keeps_asking_for_what_is_new_only() -> None:
    """The series of the service's library that was not followed from the sheet is unchanged:
    its watermark is the highest chapter, the first cycle asks for nothing."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        five_chapters(bench)
        cycles(bench)
        assert requested_numbers(bench) == []


@pytest.mark.req("EF-UX-006")
def test_a_follow_files_the_edition_on_the_shelf_that_was_chosen() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        other = new_ulid()
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.insert(export.shelf).values(
                    id=other, name="Ados", phrase="Mes mangas", max_age_rating=16
                )
            )
        assert other != bench.shelf_id
        follow(bench, FollowIntent(mode="new_only", shelf_id=other))
        cycles(bench, 1)
        shelves = {
            r["remote"]: r["shelf"]
            for r in bench.rows(
                "SELECT i.remote_id AS remote, e.shelf_id AS shelf FROM source_binding b "
                "JOIN source_item i ON i.id = b.source_item_id JOIN edition e ON e.id = b.edition_id"
            )
        }
        assert shelves["/series-alpha"] == other
        assert shelves["/series-beta"] == bench.shelf_id  # the other series is filed as ever


@pytest.mark.req("EF-UX-006")
def test_a_follow_without_a_shelf_is_filed_where_discovery_files_it() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        follow(bench, FollowIntent(mode="all"))
        cycles(bench, 1)
        rows = bench.rows(
            "SELECT e.shelf_id AS shelf FROM source_binding b "
            "JOIN source_item i ON i.id = b.source_item_id JOIN edition e ON e.id = b.edition_id "
            "WHERE i.remote_id = '/series-alpha'"
        )
        assert [r["shelf"] for r in rows] == [bench.shelf_id]


# -- the estimate ---------------------------------------------------------------------------------


def chapters(count: int) -> list[UnitInfo]:
    return [
        UnitInfo(
            remote_unit_id=f"ch-{n}", source_order=n - 1, number=str(n), number_sort=Decimal(n)
        )
        for n in range(1, count + 1)
    ]


def alpha(units: list[UnitInfo] | None = None, **extra: Any) -> FakeSource:
    return FakeSource(
        "alpha", name="Alpha texts", pages=[[hit("a1", "A Quiet Tale")]],
        units=units if units is not None else chapters(6), **extra,
    )  # fmt: skip


def hold(settings: Settings, numbers: list[str]) -> None:
    """The library already holds these chapters of alpha / a1, as readable copies."""
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            work_id = add_work(connection)
            edition_id = add_edition(connection, work_id)
            item_id, binding_id = new_ulid(), new_ulid()
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item_id, plugin_id="alpha", source_key="alpha", remote_id="a1"
                )
            )
            connection.execute(
                sa.insert(acquisition.source_binding).values(
                    id=binding_id, work_id=work_id, edition_id=edition_id,
                    source_item_id=item_id, priority=1, active=True,
                )
            )  # fmt: skip
            for number in numbers:
                unit_id = add_unit(connection, edition_id, number)
                connection.execute(
                    sa.insert(replica_schema.replica).values(
                        id=new_ulid(), unit_id=unit_id, location="store",
                        rel_path=f"alpha/{number}.cbz",
                    )
                )
    finally:
        engine.dispose()


@pytest.mark.req("EF-UX-006")
def test_estimate_of_new_only_counts_what_the_library_does_not_hold(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        assert api.login().status_code == 200
        hold(settings, ["1", "2"])
        everything = api.client.get(ESTIMATE, params={"mode": "all"}).json()
        news = api.client.get(ESTIMATE, params={"mode": "new_only"}).json()
        later = api.client.get(ESTIMATE, params={"mode": "from_number", "from_number": "5"}).json()
        assert everything["units"] == 6  # what the source lists
        assert news["units"] == 4  # chapters 3 to 6
        assert later["units"] == 2  # 5 and 6
        assert news["size_bytes"] < everything["size_bytes"]


@pytest.mark.req("EF-UX-006")
def test_estimate_of_a_series_the_library_has_never_seen_holds_nothing_back(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        assert api.login().status_code == 200
        news = api.client.get(ESTIMATE, params={"mode": "new_only"}).json()
        assert news["units"] == 6


@pytest.mark.req("EF-UX-006")
@pytest.mark.parametrize(
    "failure", [SourceError("down"), RuntimeError("boom")], ids=["source-error", "unexpected"]
)
def test_estimate_of_a_source_that_cannot_list_is_an_error_never_zero(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, failure: Exception
) -> None:
    with instance(monkeypatch, settings, alpha(list_error=failure)) as api:
        assert api.login().status_code == 200
        answer = api.client.get(ESTIMATE, params={"mode": "all"})
        assert answer.status_code == 502, answer.text
        assert answer.headers["content-type"].startswith("application/problem+json")
        assert "units" not in answer.json()


# -- the confirmation, the shelf and the doors ----------------------------------------------------

BIG = 150  # above the 100 Units that ask for a confirmation


def post(api: Any, **body: object) -> Any:
    return api.client.post(
        FOLLOWS,
        json={"source_key": "alpha", "remote_id": "a1", **body},
        headers={CSRF_HEADER: api.csrf()},
    )


@pytest.mark.req("EF-UX-006", "EF-EXP-053")
def test_a_stale_confirmation_is_refused_with_the_fresh_estimate(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha(chapters(BIG))) as api:
        assert api.login().status_code == 200
        seen = api.client.get(ESTIMATE, params={"mode": "all"}).json()
        assert seen["needs_confirmation"] is True and seen["units"] == BIG

        for stale in (
            {"confirm_units": seen["units"] - 1, "confirm_bytes": seen["size_bytes"]},
            {"confirm_units": seen["units"], "confirm_bytes": seen["size_bytes"] + 1},
            {"confirm_units": seen["units"] + 1, "confirm_bytes": seen["size_bytes"]},  # made up
            {},  # nothing was confirmed at all
        ):
            answer = post(api, mode="all", **stale)
            assert answer.status_code == 409, (stale, answer.text)
            body = answer.json()
            assert body["code"] == "backfill.confirmation_required"
            assert body["units"] == seen["units"] and body["size_bytes"] == seen["size_bytes"]
            assert body["needs_confirmation"] is True


@pytest.mark.req("EF-UX-006", "EF-EXP-053")
def test_a_confirmation_that_is_what_was_estimated_goes_on(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha(chapters(BIG))) as api:
        assert api.login().status_code == 200
        seen = api.client.get(ESTIMATE, params={"mode": "all"}).json()
        answer = post(
            api, mode="all", confirm_units=seen["units"], confirm_bytes=seen["size_bytes"]
        )
        # no service is connected in this instance: that is the refusal it gets, not a stale one
        assert answer.status_code != 409 or answer.json()["code"] != "backfill.confirmation_required"


@pytest.mark.req("EF-UX-006")
def test_a_small_choice_asks_for_no_confirmation(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha(chapters(3))) as api:
        assert api.login().status_code == 200
        answer = post(api, mode="all")
        assert answer.status_code != 409 or answer.json()["code"] != "backfill.confirmation_required"


@pytest.mark.req("EF-UX-006")
def test_a_shelf_that_does_not_exist_is_refused_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha(chapters(3))) as api:
        assert api.login().status_code == 200
        answer = post(api, mode="all", shelf_id="01J00000000000000000NOSHELF")
        assert answer.status_code == 404, answer.text
        connection = sqlite3.connect(settings.database_path)
        try:
            assert connection.execute("SELECT count(*) FROM follow_request").fetchone() == (0,)
        finally:
            connection.close()


@pytest.mark.req("EF-UX-006", "ENF-SEC-010")
def test_a_key_is_refused_the_follow_and_the_estimate_like_a_member_and_a_child(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha(chapters(3))) as api:
        for who, ip in (("member", "10.0.2.61"), ("kid", "10.0.2.62")):
            key = api_key_of(api, who)
            keyed = api.other_client(ip)
            headers = {"Authorization": f"Bearer {key}"}
            body = {"source_key": "alpha", "remote_id": "a1", "mode": "all"}
            assert keyed.post(FOLLOWS, json=body, headers=headers).status_code in (401, 403), who
            assert keyed.get(ESTIMATE, params={"mode": "all"}, headers=headers).status_code == 403
