"""Contradictor review, blocker 1: a discovered series never exports without an AgeRating.

A series found in the Suwayomi library is unrated (invisible to a child profile, EF-USR-015). The
ComicInfo of its files must still say how adult it is, since Kavita's own profiles and its
"include unknowns" switch read it: `Adults Only 18+` on an adult shelf, `X18+` when the source
flagged the sub-source as adult-only (EF-EXP-011).
"""

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
