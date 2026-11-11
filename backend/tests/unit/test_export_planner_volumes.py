"""The planner and the volumes of a web novel (M2 L26b, EF-BIB-044, EF-BIB-040, EF-EXP-040).

An export item of a range is planned like a file of its own, by the same function: the family
barrier, the age gate of an EPUB series, the names, one file per Unit. Pure, no database."""

import pytest

from shelfhold.export.domain.naming import FrozenAttrs
from shelfhold.export.domain.planner import (
    AGE_WAIT,
    AgeGateFacts,
    DestinationFacts,
    EditionFacts,
    PlanRequest,
    RecordFacts,
    UnitFacts,
    VolumeFacts,
    plan_destination,
)
from tests.fakes.kavita.plugin_parse import simulate_parse

LIBRARY = "01JLIBRARY00000000000000A1"
HASH = "sha256:" + "a" * 64
WARNING = {"wait": "volume_waits", "refuse": "volume_refused"}
pytestmark = pytest.mark.req("EF-BIB-044", "EF-BIB-040")


def destination(**extra: object) -> DestinationFacts:
    values: dict[str, object] = {
        "shelf_destination_id": "dest-1",
        "shelf_id": "shelf-1",
        "remote_library_id": LIBRARY,
    }
    return DestinationFacts(**{**values, **extra})  # type: ignore[arg-type]


def volume(item: str = "v-1", label: str | None = "Vol.1", **extra: object) -> VolumeFacts:
    values: dict[str, object] = {
        "export_item_id": item,
        "edition_id": "ed-1",
        "label": label,
        "closed": True,
        "unit_ids": ("u-1", "u-2"),
        "content_hash": HASH,
    }
    return VolumeFacts(**{**values, **extra})  # type: ignore[arg-type]


def request(**extra: object) -> PlanRequest:
    values: dict[str, object] = {
        "destination": destination(),
        "editions": (EditionFacts("ed-1", "w-1", "Le Roman", "fr"),),
        "units": (),
        "volumes": (volume(),),
    }
    return PlanRequest(**{**values, **extra})  # type: ignore[arg-type]


def chapter(unit_id: str, number: str, **extra: object) -> UnitFacts:
    return UnitFacts(unit_id, "ed-1", number, content_hash=HASH, **extra)  # type: ignore[arg-type]


def test_closed_volume_is_planned_by_item_with_no_unit() -> None:
    plan = plan_destination(request(), simulate_parse)
    (op,) = plan.ops
    assert (op.kind, op.unit_id, op.export_item_id) == ("write", None, "v-1")
    assert op.rel_path is not None and op.rel_path.endswith(" - Vol.1.epub")
    assert op.frozen["kind"] == "volume" and op.frozen["number_norm"] == "1"
    assert op.content_hash == HASH


def test_units_a_volume_covers_are_removed_from_the_plan_by_unit() -> None:
    units = (chapter("u-1", "1"), chapter("u-2", "2"), chapter("u-3", "3"))
    plan = plan_destination(request(units=units), simulate_parse)
    assert sorted((op.unit_id, op.export_item_id) for op in plan.ops if op.unit_id) == [
        ("u-3", None)
    ]
    assert [op.export_item_id for op in plan.ops if op.unit_id is None] == ["v-1"]


def test_units_of_a_volume_already_written_are_not_planned_either() -> None:
    written = RecordFacts(
        "v-1", None, "written", "Le Roman/Le Roman - Vol.1.epub", HASH, None,
        FrozenAttrs("Le Roman", "main", "1", kind="volume"), kind="volume",
        unit_ids=("u-1", "u-2"),
    )  # fmt: skip
    plan = plan_destination(
        request(units=(chapter("u-1", "1"),), records=(written,)), simulate_parse
    )
    assert plan.ops == ()


@pytest.mark.parametrize("verdict", ["wait", "refuse"])
def test_volume_the_policy_does_not_let_go_is_not_planned(verdict: str) -> None:
    plan = plan_destination(
        request(volumes=(volume(closed=False, verdict=verdict),)), simulate_parse
    )
    assert plan.ops == () and [w.code for w in plan.warnings] == [WARNING[verdict]]


def test_missing_file_is_blocked_not_silently_skipped() -> None:
    plan = plan_destination(request(volumes=(volume(content_hash=None),)), simulate_parse)
    assert plan.ops == () and [b.code for b in plan.blocked] == ["export.ranged_source_missing"]
    assert [b.export_item_id for b in plan.blocked] == ["v-1"]


@pytest.mark.parametrize(
    ("label", "number"),
    [("Vol.3", "3"), ("Volume 12", "12"), ("Tome 04", "4"), ("Book 2.5", "2.5")],
)
def test_volume_number_is_read_from_the_label(label: str, number: str) -> None:
    (op,) = plan_destination(request(volumes=(volume(label=label),)), simulate_parse).ops
    assert op.frozen["number_norm"] == number


@pytest.mark.parametrize("label", [None, "", "Prologue"])
def test_volume_without_a_number_is_blocked(label: str | None) -> None:
    plan = plan_destination(request(volumes=(volume(label=label),)), simulate_parse)
    assert plan.ops == () and [b.code for b in plan.blocked] == ["export.volume_unnamed"]


def test_family_shelf_takes_a_volume_only_for_an_allowed_edition() -> None:
    family = destination(max_age_rating=13, partitioning="library")
    for verdict, code in (
        ("above_ceiling", "shelf.above_ceiling"),
        ("unrated_edition", "shelf.rating_required"),
        ("suggestion_above", "shelf.suggestion_above"),
    ):
        plan = plan_destination(
            request(destination=family, admissions={"ed-1": verdict}), simulate_parse
        )
        assert plan.ops == () and [b.code for b in plan.blocked] == [code]
    silent = plan_destination(request(destination=family), simulate_parse)  # nothing said: closed
    assert silent.ops == () and [b.code for b in silent.blocked] == ["shelf.rating_required"]
    allowed = plan_destination(
        request(destination=family, admissions={"ed-1": "allowed"}), simulate_parse
    )
    assert len(allowed.ops) == 1


def test_family_shelf_refusal_of_the_whole_destination_covers_volumes() -> None:
    family = destination(max_age_rating=13, partitioning="none")
    plan = plan_destination(request(destination=family), simulate_parse)
    assert plan.ops == () and [b.code for b in plan.blocked] == [
        "shelf.destination_not_partitioned"
    ]


def test_volume_goes_through_the_age_gate_of_an_epub_series() -> None:
    wanted = {"age_wanted": frozenset({"ed-1"})}
    closed = plan_destination(request(**wanted), simulate_parse)  # nothing read from Kavita
    assert closed.ops == () and [b.code for b in closed.blocked] == [AGE_WAIT]
    gate = AgeGateFacts(open=True)
    first = plan_destination(request(age_gate=gate, **wanted), simulate_parse)
    assert len(first.ops) == 1  # the first EPUB of a series goes alone
    two = request(
        age_gate=gate, volumes=(volume("v-1"), volume("v-2", "Vol.2", unit_ids=("u-3",))), **wanted
    )
    plan = plan_destination(two, simulate_parse)
    assert len(plan.ops) == 1 and [b.code for b in plan.blocked] == [AGE_WAIT]
    exposed = plan_destination(
        request(age_gate=gate, age_unknown_exposed=frozenset({"ed-1"}), **wanted), simulate_parse
    )
    assert exposed.ops == () and [b.code for b in exposed.blocked] == [AGE_WAIT]


def test_volume_of_a_series_whose_age_reads_back_locked_goes() -> None:
    plan = plan_destination(request(age_wanted=frozenset({"ed-1"})), simulate_parse)
    assert plan.ops == ()
    # the series is the one the exporter made for the Edition: verified by its identifier
    from shelfhold.export.domain.planner import SeriesFacts

    series = SeriesFacts("s-1", LIBRARY, "Le Roman", "le roman", edition_ids=frozenset({"ed-1"}))
    verified = request(
        series=(series,),
        edition_series={"ed-1": "s-1"},
        age_wanted=frozenset({"ed-1"}),
        age_gate=AgeGateFacts(verified=frozenset({"s-1"})),
    )
    assert len(plan_destination(verified, simulate_parse).ops) == 1


def test_open_tail_changed_since_it_was_served_is_planned_again_only_when_it_rolls() -> None:
    written = RecordFacts(
        "v-1", None, "written", "Le Roman/Le Roman - Vol.1.epub", "sha256:" + "0" * 64, None,
        FrozenAttrs("Le Roman", "main", "1", kind="volume"), kind="volume",
        unit_ids=("u-1", "u-2"),
    )  # fmt: skip
    rolling = request(records=(written,), volumes=(volume(closed=False, verdict="export_tail"),))
    (op,) = plan_destination(rolling, simulate_parse).ops
    assert op.rel_path == written.rel_path and op.export_item_id == "v-1"  # the same path
    same = RecordFacts(**{**written.__dict__, "content_hash": HASH})
    assert (
        plan_destination(request(records=(same,), volumes=rolling.volumes), simulate_parse).ops
        == ()
    )
    frozen = request(records=(written,), volumes=(volume(closed=True),))
    assert plan_destination(frozen, simulate_parse).ops == ()  # a closed volume never again
    waits = request(records=(written,), volumes=(volume(closed=False, verdict="wait"),))
    assert plan_destination(waits, simulate_parse).ops == ()
