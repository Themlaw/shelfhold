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

from tests.follow_env import Bench, add_adult_shelf, follow_bench
from tests.pairing_env import paired_env

LOCALE = Path(__file__).resolve().parents[2] / "src" / "shelfhold" / "locales" / "fr.toml"
COUNTS = {
    "work": "SELECT count(*) AS n FROM work",
    "edition": "SELECT count(*) AS n FROM edition",
    "source_item": "SELECT count(*) AS n FROM source_item",
    "source_binding": "SELECT count(*) AS n FROM source_binding",
    "series_follow": "SELECT count(*) AS n FROM series_follow",
}


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
        first = bench.run()
        assert first.created == 1 and first.tombstoned == 1
        assert set(_series(bench)) == {"/series-beta"}
        bench.run()
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
        report = bench.run()
        assert report.created == 0 and report.awaiting_shelf == 2
        assert _counts(bench) == dict.fromkeys(COUNTS, 0)
        bench.run()
        told = bench.events("series.awaiting_shelf")
        assert len(told) == 1
        locale = tomllib.loads(LOCALE.read_text("utf-8"))
        assert "shelfhold shelf create" in locale["errors"]["series.awaiting_shelf"]["action"]
        bench.shelf_id = ""  # the admin creates the shelf: the next cycle files the series
        add_adult_shelf(bench.lib)
        assert bench.run().created == 2


@pytest.mark.req("EF-SRC-009")
def test_series_with_unknown_sub_source_language_is_not_created() -> None:
    """A sub-source that is not of one language (`all`) gives no Edition key: the series waits."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.server.state["mangas"][2]["inLibrary"] = True  # Series Gamma, source 7003 (`all`)
        bench.server.state["mangas"][2]["chapters"][0]["chapterNumber"] = 1.0
        report = bench.run()
        assert report.created == 2 and report.skipped_language == 1
        assert "/series-gamma" not in _series(bench)
