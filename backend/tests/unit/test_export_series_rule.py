"""EF-BIB-025: the Kavita series of an Edition is its Series when there is one, else its Work."""

import pytest

from shelfhold.export.domain.planner import (
    DestinationFacts,
    EditionFacts,
    PlanRequest,
    UnitFacts,
    plan_destination,
)
from tests.fakes.kavita.plugin_parse import simulate_parse

LIBRARY = "01JLIBRARY00000000000000A1"
CYCLE = "01JSERIES0000000000000CYCLE"


def album(n: int, *, series: bool = True, lang: str = "fr") -> EditionFacts:
    return EditionFacts(
        edition_id=f"ed-{n}-{lang}", work_id=f"w-{n}", work_title=f"Album {n}", lang=lang,
        series_id=CYCLE if series else None, series_title="Le Cycle" if series else None,
    )  # fmt: skip


def unit(edition: EditionFacts, number: str) -> UnitFacts:
    return UnitFacts(
        unit_id=f"u-{edition.edition_id}", edition_id=edition.edition_id, number_norm=number,
        content_hash="sha256:" + "a" * 64,
    )


def plan(*editions: EditionFacts, numbers: tuple[str, ...] = ("1", "2", "3")):  # type: ignore[no-untyped-def]
    units = tuple(unit(e, n) for e, n in zip(editions, numbers, strict=False))
    request = PlanRequest(
        destination=DestinationFacts("dest-1", "shelf-1", remote_library_id=LIBRARY),
        editions=editions, units=units,
    )  # fmt: skip
    return plan_destination(request, simulate_parse)


@pytest.mark.req("EF-BIB-025")
def test_kavita_series_is_series_when_present_else_work() -> None:
    albums = [album(n) for n in (1, 2, 3)]
    planned = plan(*albums)
    assert planned.blocked == ()  # three albums of one Series are not a name collision
    assert {op.series.name_raw for op in planned.ops} == {"Le Cycle"}
    assert len({op.rel_path.split("/")[0] for op in planned.ops if op.rel_path}) == 1
    alone = plan(album(1, series=False))
    assert {op.series.name_raw for op in alone.ops} == {"Album 1"}  # no Series: the Work's title


@pytest.mark.req("EF-BIB-025")
def test_series_members_in_two_languages_get_the_language_suffix() -> None:
    planned = plan(album(1), album(2), album(1, lang="en"), numbers=("1", "2", "1"))
    assert planned.blocked == ()
    assert {op.series.name_raw for op in planned.ops} == {"Le Cycle (fr)", "Le Cycle (en)"}


@pytest.mark.req("EF-BIB-025")
def test_a_work_called_like_a_series_is_still_a_collision() -> None:
    lone = EditionFacts("ed-x", "w-x", "Le Cycle", "fr")
    planned = plan(album(1), lone, numbers=("1", "1"))
    assert planned.ops == ()
    assert {b.code for b in planned.blocked} == {"export.series_name_collision"}
