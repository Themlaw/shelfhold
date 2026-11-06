"""Contradictor review, blocker 1: a discovered series never exports without an AgeRating.

A series found in the Suwayomi library is unrated (invisible to a child profile, EF-USR-015). The
ComicInfo of its files must still say how adult it is, since Kavita's own profiles and its
"include unknowns" switch read it: `Adults Only 18+` on an adult shelf, `X18+` when the source
flagged the sub-source as adult-only (EF-EXP-011).
"""

import re

import pytest
import sqlalchemy as sa

from shelfhold.export.application.rendering import comicinfo_data
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.packaging.comicinfo import render_comicinfo
from tests.follow_env import Bench, follow_bench

pytestmark = pytest.mark.req("EF-EXP-011", "EF-SRC-009")


def _xml_of_first_unit(bench: Bench, edition_id: str) -> str:
    with bench.lib.engine.begin() as connection:
        unit_id = new_ulid()
        connection.execute(
            sa.text(
                "INSERT INTO unit (id, edition_id, track, number_segment, number_raw, number_norm,"
                " part, source_order) VALUES (:i, :e, 'main', '', '1', '1', 1, 1)"
            ),
            {"i": unit_id, "e": edition_id},
        )
        return render_comicinfo(comicinfo_data(connection, unit_id, "Series")).xml.decode()


def test_a_discovered_series_exports_adults_only_not_a_missing_age() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        works = bench.rows("SELECT age_rating, age_x FROM work")
        assert works and all(w["age_rating"] is None and not w["age_x"] for w in works)  # unrated
        editions = bench.rows("SELECT id FROM edition")
        xml = _xml_of_first_unit(bench, editions[0]["id"])
        assert "<AgeRating>Adults Only 18+</AgeRating>" in xml


def test_a_nsfw_sub_source_exports_x18() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        for source in bench.server.state["sources"]:
            source["isNsfw"] = True
        bench.run()
        suggestions = bench.rows("SELECT value, x, provenance FROM age_suggestion")
        assert suggestions and all(
            (s["value"], s["x"], s["provenance"]) == (18, 1, "sub_source") for s in suggestions
        )
        assert (
            bench.rows("SELECT count(*) AS n FROM work WHERE age_rating IS NOT NULL")[0]["n"] == 0
        )
        editions = bench.rows("SELECT id FROM edition")
        assert "<AgeRating>X18+</AgeRating>" in _xml_of_first_unit(bench, editions[0]["id"])


def test_a_rated_work_keeps_its_own_age() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        with bench.lib.engine.begin() as connection:
            connection.execute(sa.text("UPDATE work SET age_rating = 16"))
        editions = bench.rows("SELECT id FROM edition")
        assert "<AgeRating>Mature 17+</AgeRating>" in _xml_of_first_unit(bench, editions[0]["id"])


# -- review of PR #69, minor 5: an X from any adopted source, and the Edition's own rating -------


def _age_of(bench: Bench, edition_id: str) -> str | None:
    units = bench.rows(f"SELECT id FROM unit WHERE edition_id = '{edition_id}'")  # noqa: S608
    with bench.lib.engine.begin() as connection:
        unit_id = units[0]["id"] if units else _first_unit(connection, edition_id)
        xml = render_comicinfo(comicinfo_data(connection, unit_id, "Series")).xml.decode()
    match = re.search(r"<AgeRating>(.*?)</AgeRating>", xml)
    return match.group(1) if match else None


def _first_unit(connection: sa.Connection, edition_id: str) -> str:
    unit_id = new_ulid()
    connection.execute(
        sa.text(
            "INSERT INTO unit (id, edition_id, track, number_segment, number_raw, number_norm,"
            " part, source_order) VALUES (:i, :e, 'main', '', '1', '1', 1, 1)"
        ),
        {"i": unit_id, "e": edition_id},
    )
    return unit_id


def _suggest(bench: Bench, edition_id: str, provenance: str, value: int, x: int) -> None:
    with bench.lib.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO age_suggestion (id, entity_type, entity_id, value, x, provenance,"
                " seen_at) VALUES (:i, 'edition', :e, :v, :x, :p, '2026-11-05T08:00:00Z')"
            ),
            {"i": new_ulid(), "e": edition_id, "v": value, "x": x, "p": provenance},
        )


def test_an_x_from_an_adopted_comicinfo_exports_x18_and_a_mild_one_does_not_lower() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        edition_id = bench.rows("SELECT id FROM edition")[0]["id"]
        _suggest(bench, edition_id, "comicinfo", 0, 0)  # "Everyone" is only a suggestion
        _suggest(bench, edition_id, "provider", 0, 0)
        assert _age_of(bench, edition_id) == "Adults Only 18+"
        _suggest(bench, edition_id, "comicinfo", 18, 1)
        assert _age_of(bench, edition_id) == "X18+"


def test_the_edition_own_rating_counts_with_the_work() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        edition_id = bench.rows("SELECT id FROM edition")[0]["id"]
        with bench.lib.engine.begin() as connection:
            connection.execute(sa.text("UPDATE edition SET age_rating = 18, age_x = 1"))
        assert _age_of(bench, edition_id) == "X18+"  # the Work is unrated
        with bench.lib.engine.begin() as connection:
            connection.execute(sa.text("UPDATE edition SET age_x = 0, age_rating = 12"))
            connection.execute(sa.text("UPDATE work SET age_rating = 16"))
        assert _age_of(bench, edition_id) == "Mature 17+"  # the stricter of the two
