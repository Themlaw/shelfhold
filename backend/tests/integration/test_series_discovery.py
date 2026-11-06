"""L42: the follow cycle discovers the series of the Suwayomi library (EF-SRC-009).

A fresh installation holds no Work and no binding. At the start of each cycle, a series of the
library that has neither a binding nor a `series_binding` tombstone gets, in one transaction, its
Work, its Edition (the language of its sub-source, on the adult shelf), its `source_item`, its
binding and its follow, with the highest visible chapter as watermark.
"""

import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import FollowReport
from shelfhold.ops.status import collect
from tests.follow_env import Bench, add_adult_shelf, follow_bench, new_chapter
from tests.pairing_env import paired_env

LOCALE = Path(__file__).resolve().parents[2] / "src" / "shelfhold" / "locales" / "fr.toml"
COUNTS = {
    "work": "SELECT count(*) AS n FROM work",
    "edition": "SELECT count(*) AS n FROM edition",
    "source_item": "SELECT count(*) AS n FROM source_item",
    "source_binding": "SELECT count(*) AS n FROM source_binding",
    "series_follow": "SELECT count(*) AS n FROM series_follow",
}


def _job_run(bench: Bench) -> FollowReport:
    """One run of the `follow.cycle` job: it builds a new `FollowCycle` each time (runtime_jobs)."""
    return asyncio.run(bench.new_cycle().run())


def _counts(bench: Bench) -> dict[str, int]:
    return {name: bench.rows(sql)[0]["n"] for name, sql in COUNTS.items()}


def _series(bench: Bench) -> dict[str, dict[str, Any]]:
    rows = bench.rows(
        "SELECT i.remote_id AS remote, w.title AS title, e.lang AS lang, e.shelf_id AS shelf, "
        "b.active AS active, f.follow_from_segment AS segment, f.follow_from_number AS number "
        "FROM source_binding b JOIN source_item i ON i.id = b.source_item_id "
        "JOIN edition e ON e.id = b.edition_id JOIN work w ON w.id = b.work_id "
        "JOIN series_follow f ON f.edition_id = b.edition_id"
    )
    return {r["remote"]: r for r in rows}


@pytest.mark.req("EF-SRC-009")
def test_library_series_without_binding_gets_work_edition_binding_once() -> None:
    """From an empty database: the two series of the library each get the five rows, once."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        assert _counts(bench) == dict.fromkeys(COUNTS, 0)
        report = bench.run()
        assert report.created == 2
        assert _counts(bench) == dict.fromkeys(COUNTS, 2)
        found = _series(bench)
        alpha, beta = found["/series-alpha"], found["/series-beta"]
        assert (alpha["title"], alpha["lang"]) == ("Series Alpha", "en")
        assert (beta["title"], beta["lang"]) == ("Series Beta", "fr")  # the sub-source's language
        assert alpha["shelf"] == beta["shelf"] == bench.shelf_id
        assert alpha["active"] and beta["active"]
        again = bench.run()
        assert again.created == 0
        assert _counts(bench) == dict.fromkeys(COUNTS, 2)


@pytest.mark.req("EF-EXP-053", "EF-SRC-009")
def test_discovered_series_first_cycle_requests_nothing() -> None:
    """The watermark is the highest visible chapter: the first cycle asks for nothing, the next
    one asks only for the chapter that appeared after."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        report = bench.run()
        assert report.requested == 0 and report.fetched == 0
        assert bench.rows("SELECT * FROM download_request") == []
        alpha = _series(bench)["/series-alpha"]
        assert (alpha["segment"], alpha["number"]) == ("", "2")
        bench.add_chapter(3)
        report = bench.run()
        assert report.requested == 1
        assert len(bench.rows("SELECT * FROM download_request")) == 1


@pytest.mark.req("EF-BIB-053", "EF-SRC-009")
def test_tombstoned_library_series_never_created() -> None:
    """A series under a `series_binding` tombstone is not created and is told once."""

    class Blocks:
        def check(self, candidate: object, *, root_id: str | None = None, **_: Any) -> Any:
            return SimpleNamespace(blocked=getattr(candidate, "series_ref", "") == "/series-alpha")

        def check_content(self, hashes: object = (), **_: Any) -> bool:
            return False

    with follow_bench(seeded=False, adult_shelf=True, tombstones=lambda _lib: Blocks()) as bench:
        first = _job_run(bench)
        assert first.created == 1 and first.tombstoned == 1
        assert set(_series(bench)) == {"/series-beta"}
        _job_run(bench)  # a new process each time: the database remembers what was told
        _job_run(bench)
        assert set(_series(bench)) == {"/series-beta"}
        told = bench.events("series.tombstoned_upstream")
        assert len(told) == 1
        assert "alpha" not in json.dumps(told).lower()  # no title in an event


@pytest.mark.req("EF-EXP-038", "EF-SRC-009")
def test_discovery_skips_series_pending_adoption_pairing() -> None:
    """An adopted, unpaired series with a candidate scoring 0.9 or more is left to the pairing:
    no second Work is created for the same series."""
    with paired_env() as p:
        p.adopt_files(["Series Alpha - Ch.1.cbz"])
        p.a.adopt()
        report = p.cycle_once()
        assert report.skipped_pending_pairing == 1
        rows = (
            p.env.lib.engine.connect()
            .exec_driver_sql(
                "SELECT i.remote_id FROM source_binding b JOIN source_item i "
                "ON i.id = b.source_item_id"
            )
            .all()
        )
        assert [r[0] for r in rows if r[0] == "/series-alpha"] == []
        # the series without any adopted twin is still discovered
        assert "/series-beta" in [r[0] for r in rows]


@pytest.mark.req("EF-SRC-009", "EF-SRC-036")
def test_discovery_idempotent_across_cycles_and_reinstall() -> None:
    """Cycles in a row, then a reinstalled tool (every local id renumbered): still one of each."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        before = _counts(bench)
        bench.run()
        bench.server.reinstall()
        report = bench.run()
        assert report.created == 0
        assert _counts(bench) == before
        assert len(_series(bench)) == 2


@pytest.mark.req("EF-SRC-009")
def test_new_cycle_object_does_not_create_twice() -> None:
    """The memory of a cycle holds nothing the database does not: a new process finds the rows."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        again = bench.new_cycle()
        assert asyncio.run(again.run()).created == 0
        assert _counts(bench) == dict.fromkeys(COUNTS, 2)


@pytest.mark.req("EF-SRC-009", "EF-INT-023")
def test_library_series_without_adult_shelf_waits_with_exit_command() -> None:
    """No adult shelf: nothing is created, the wait is told once with the command that leaves it."""
    with follow_bench(seeded=False) as bench:
        report = _job_run(bench)
        assert report.created == 0 and report.awaiting_shelf == 2
        assert _counts(bench) == dict.fromkeys(COUNTS, 0)
        _job_run(bench)
        _job_run(bench)
        told = bench.events("series.awaiting_shelf")
        assert len(told) == 1
        with bench.lib.engine.connect() as connection:
            (item,) = [i for i in collect(connection) if i.state == "awaiting_shelf"]
        assert "shelfhold shelf create" in item.command
        locale = tomllib.loads(LOCALE.read_text("utf-8"))
        assert "shelfhold shelf create" in locale["errors"]["series.awaiting_shelf"]["action"]
        bench.shelf_id = ""  # the admin creates the shelf: the next cycle files the series
        add_adult_shelf(bench.lib)
        assert bench.run().created == 2
        with bench.lib.engine.connect() as connection:  # the wait ended: it leaves the status
            assert [i for i in collect(connection) if i.state == "awaiting_shelf"] == []
        assert bench.rows("SELECT * FROM follow_notice") == []


@pytest.mark.req("EF-SRC-009", "ENF-I18N-005")
def test_series_with_unknown_sub_source_language_waits_for_set_lang() -> None:
    """A sub-source that is not of one language (`all`) gives no Edition key: the series is filed
    as `x-unknown` and `status` lists `language_unknown` with `edition set-lang`."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.server.state["mangas"][2]["inLibrary"] = True  # Series Gamma, source 7003 (`all`)
        report = bench.run()
        assert report.created == 3 and report.unknown_language == 1
        assert _series(bench)["/series-gamma"]["lang"] == "x-unknown"
        with bench.lib.engine.connect() as connection:
            (item,) = [i for i in collect(connection) if i.state == "language_unknown"]
        assert "edition set-lang" in item.command


@pytest.mark.req("EF-EXP-038", "EF-SRC-009", "EF-INT-023")
def test_twin_scoring_below_auto_threshold_is_left_to_pairing_and_listed() -> None:
    """0.75 is the proposal threshold of `adopt pair`: a look-alike title below the batch
    threshold is not created either; `status` lists it with the pairing command."""
    with paired_env() as p:
        p.server.state["mangas"][0]["title"] = "Series Alpha Reborn"
        p.adopt_files(["Series Alpha - Ch.1.cbz"])
        p.a.adopt()
        report = p.cycle_once()
        assert report.skipped_pending_pairing == 1 and report.created == 1
        remotes = [
            r["r"]
            for r in p.rows(
                "SELECT i.remote_id r FROM source_binding b JOIN "
                "source_item i ON i.id = b.source_item_id"
            )
        ]
        assert "/series-alpha" not in remotes
        (proposal,) = p.pairing.propose(p.destination, p.library())
        assert proposal.state == "ready"  # still pairable: nothing took the upstream series
        with p.env.lib.engine.connect() as connection:
            (item,) = [i for i in collect(connection) if i.state == "pending_pairing"]
        assert "adopt pair" in item.command and "--decline" in item.command


@pytest.mark.req("EF-EXP-038", "EF-SRC-009")
def test_declined_pairing_lets_the_discovery_create_the_series() -> None:
    from shelfhold.acquisition.public import decline_pairing

    with paired_env() as p:
        p.server.state["mangas"][0]["title"] = "Series Alpha Reborn"
        p.adopt_files(["Series Alpha - Ch.1.cbz"])
        p.a.adopt()
        p.cycle_once()
        (proposal,) = p.pairing.propose(p.destination, p.library())
        decline_pairing(p.env.lib.engine, p.env.clock, proposal.export_series_id)
        assert p.cycle_once().created == 1  # the declined look-alike is filed on its own
        with p.env.lib.engine.connect() as connection:
            assert [i for i in collect(connection) if i.state == "pending_pairing"] == []


@pytest.mark.req("EF-EXP-053", "EF-SRC-009")
def test_archive_that_appears_after_an_extras_only_look_is_not_requested() -> None:
    """No numbered chapter at discovery: the watermark stays null; when 40 numbered chapters show
    up, the first numbered look takes the highest one and nothing is asked for."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        mangas = bench.server.state["mangas"]
        extras = [
            dict(c, chapterNumber=-1.0, name=f"Extra {i}")
            for i, c in enumerate(mangas[1]["chapters"])
        ]
        mangas[1]["chapters"] = extras
        assert bench.run().requested == 0
        assert _series(bench)["/series-beta"]["number"] is None  # not "-1": nothing numbered yet
        mangas[1]["chapters"] = [
            dict(new_chapter(9000 + n, n, url=f"/series-beta/ch-{n:03d}"), sourceOrder=n - 1)
            for n in range(1, 41)
        ]
        total = sum(bench.run().requested for _ in range(3))
        assert total == 0
        assert _series(bench)["/series-beta"]["number"] == "40"
        mangas[1]["chapters"].append(
            dict(new_chapter(9041, 41, url="/series-beta/ch-041"), sourceOrder=40)
        )
        assert bench.run().requested == 1


@pytest.mark.req("EF-SRC-009")
def test_discovery_lists_each_new_series_once() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        listings = [
            r for r in bench.server.requests if r.path == "/api/graphql" and "chapters" in r.fields
        ]
        per_manga = len(listings)
        assert per_manga <= 2  # one `chapters` query per new series, not two


@pytest.mark.req("EF-SRC-009", "ENF-SEC-015")
@pytest.mark.parametrize("bad", ["", "   ", "x" * 20000, "\u202eEvil\x00name https://x.y"])
def test_upstream_title_is_cleaned_bounded_and_never_empty(bad: str) -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.server.state["mangas"][0]["title"] = bad
        assert bench.run().created == 2
        (row,) = bench.rows(
            "SELECT w.title t FROM work w JOIN source_binding b ON b.work_id = w.id "
            "JOIN source_item i ON i.id = b.source_item_id "
            "WHERE i.remote_id = '/series-alpha'"
        )
        assert 0 < len(row["t"]) <= 200
        assert "\x00" not in row["t"] and "\u202e" not in row["t"]
        if not bad.strip():
            assert row["t"] == "/series-alpha"  # the remote identifier stands in


@pytest.mark.req("EF-SRC-009")
def test_one_active_binding_per_upstream_series_in_the_schema() -> None:
    """The database itself refuses a second active binding on one source_item."""
    from shelfhold.catalog.public import Catalog, EditionSpec
    from shelfhold.infrastructure.ids import new_ulid
    from shelfhold.infrastructure.schema import acquisition

    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        item = bench.rows("SELECT source_item_id i FROM source_binding LIMIT 1")[0]["i"]
        with pytest.raises(sa.exc.IntegrityError), bench.lib.engine.begin() as c:
            w = Catalog(c).create_work("manga", "Dup")
            e = Catalog(c).ensure_edition(
                EditionSpec(w, "en", translation_type="official", media="images")
            )
            c.execute(
                sa.insert(acquisition.source_binding).values(
                    id=new_ulid(),
                    work_id=w,
                    edition_id=e.id,
                    source_item_id=item,
                    priority=1,
                    active=True,
                )
            )
