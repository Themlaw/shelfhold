"""M1b L18: a Suwayomi that was reinstalled is told by its fingerprint and the follow pauses, with
no cascade (EF-SRC-036, EF-INT-023); `source reconnect` is the way out."""

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.instance import reconnect
from shelfhold.infrastructure.schema import catalog
from tests.fakes.suwayomi import FakeSuwayomi
from tests.follow_env import Bench, follow_bench

COUNTS = (
    "SELECT (SELECT count(*) FROM download_request) AS tickets,"
    " (SELECT count(*) FROM replica) AS replicas,"
    " (SELECT count(*) FROM series_follow) AS follows,"
    " (SELECT count(*) FROM source_binding WHERE active) AS active,"
    " (SELECT count(*) FROM source_failure) AS failures"
)


def _installed() -> FakeSuwayomi:
    server = FakeSuwayomi(auth_mode="none")
    server.assign("/series-alpha", server.add_category("Shelfhold", include_in_download="EXCLUDE"))
    return server


def _stored(bench: Bench) -> str | None:
    rows = bench.rows("SELECT instance_fingerprint AS fp FROM source_config")
    return rows[0]["fp"] if rows else None


@pytest.mark.req("EF-SRC-036", "EF-INT-023")
def test_connected_service_reinstall_pauses_without_cascade() -> None:
    server = _installed()
    with follow_bench(server) as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()  # a ticket is open
        first = _stored(bench)
        assert first and bench.rows("SELECT instance_fingerprint_seen_at AS at FROM source_config")
        before = bench.rows(COUNTS)[0]
        assert before["tickets"] == 1

        server.reinstall(wipe=True)  # the tool is installed again: an empty library
        report = bench.run()
        assert report.paused == "source_instance_changed"
        assert bench.rows(COUNTS)[0] == before  # nothing removed, nothing asked again
        assert bench.binding()["active"] and bench.binding()["disabled_reason"] is None
        assert _stored(bench) == first  # the old fingerprint is kept until the admin decides
        assert [e["state"] for e in bench.events("source.instance_changed")] == [
            "source_instance_changed"
        ]
        assert not [r for r in server.requests if "deleteDownloadedChapter" in r.fields]
        assert not [r for r in server.requests if "createCategory" in r.fields]

        sent = len(server.requests)
        again = bench.run()  # paused: no request, no second alert
        assert again.paused == "source_instance_changed" and len(server.requests) == sent
        assert len(bench.events("source.instance_changed")) == 1
        assert bench.rows(COUNTS)[0] == before and server.violations == []


@pytest.mark.req("EF-SRC-036", "EF-INT-023")
def test_reconnect_leaves_the_pause_and_follows_by_stable_identity() -> None:
    server = _installed()
    with follow_bench(server) as bench:
        bench.run()
        server.reinstall(wipe=True)
        assert bench.run().paused == "source_instance_changed"
        before = bench.rows(COUNTS)[0]

        server.assign(
            "/series-alpha", server.add_category("Shelfhold", include_in_download="EXCLUDE")
        )
        bench.alpha()["inLibrary"] = True  # the admin put the series back in the new Suwayomi
        with bench.lib.engine.begin() as connection:
            assert reconnect(connection, "suwayomi", bench.clock.now()) is True
            assert (
                reconnect(connection, "suwayomi", bench.clock.now()) is False
            )  # nothing left to leave
        assert _stored(bench) is None
        resumed = bench.run()
        assert resumed.paused is None and resumed.removed == 0
        assert _stored(bench)  # the new installation is the reference now
        assert bench.rows(COUNTS)[0] == before and bench.binding()["active"]


@pytest.mark.req("EF-SRC-036")
def test_reconnect_does_not_leave_a_pause_it_did_not_cause() -> None:
    server = FakeSuwayomi(auth_mode="basic_auth")
    with follow_bench(server, has_credentials=True) as bench:
        assert bench.run().paused == "auth_expired"
        with bench.lib.engine.begin() as connection:
            assert reconnect(connection, "suwayomi", bench.clock.now()) is False
        assert bench.run().paused == "auth_expired"  # still waiting for `source set-secret`


@pytest.mark.req("EF-SRC-036")
def test_a_first_look_stores_the_fingerprint_and_a_missing_category_alone_pauses_nothing() -> None:
    with follow_bench(FakeSuwayomi(auth_mode="none")) as bench:
        assert bench.run().paused is None  # no Shelfhold category yet: nothing to compare
        assert not _stored(bench)


@pytest.mark.req("EF-SRC-036")
def test_reconnect_purges_orphan_tickets_and_fetch_handles_only_then() -> None:
    server = _installed()
    with follow_bench(server) as bench:
        bench.run()
        bench.add_chapter(3)
        bench.run()  # a ticket is open on this installation
        with bench.lib.engine.begin() as connection:  # and a handle is cached
            connection.execute(
                sa.update(catalog.unit_variant).values(
                    fetch_handle="5030", fetch_handle_seen_at=bench.clock.now()
                )
            )
        open_ticket = bench.rows("SELECT ticket, state FROM download_request")[0]
        assert open_ticket["ticket"] and open_ticket["state"] == "requested"

        server.reinstall(wipe=True)
        assert bench.run().paused == "source_instance_changed"
        # the detection purged nothing
        assert bench.rows("SELECT ticket FROM download_request")[0]["ticket"]
        assert bench.rows("SELECT count(*) AS n FROM unit_variant WHERE fetch_handle IS NOT NULL")[
            0
        ]["n"]

        with bench.lib.engine.begin() as connection:
            assert reconnect(connection, "suwayomi", bench.clock.now())
        rows = bench.rows("SELECT ticket, state, attempts FROM download_request")
        assert [(r["ticket"], r["state"], r["attempts"]) for r in rows] == [(None, "requested", 0)]
        assert (
            bench.rows("SELECT count(*) AS n FROM unit_variant WHERE fetch_handle IS NOT NULL")[0][
                "n"
            ]
            == 0
        )
        assert bench.rows("SELECT count(*) AS n FROM unit_variant")[0]["n"]  # the Variants stay
