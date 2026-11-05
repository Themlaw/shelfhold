"""The planner and the binding rules, as pure functions (EF-EXP-002, EF-EXP-058, EF-EXP-062)."""

import pytest

from shelfhold.export.domain.bindings import (
    BindingFacts,
    PullNeedsReadableCopyError,
    ShelfBelowEighteenError,
    TreeOverlapError,
    check_binding,
    check_shelf_ceiling,
    resolve_egress,
)
from shelfhold.export.domain.planner import (
    DestinationFacts,
    EditionFacts,
    PlanRequest,
    SeriesFacts,
    UnitFacts,
    plan_destination,
)
from tests.fakes.kavita.plugin_parse import simulate_parse

LIBRARY = "01JLIBRARY00000000000000A1"


def destination(**extra: object) -> DestinationFacts:
    values: dict[str, object] = {
        "shelf_destination_id": "dest-1",
        "shelf_id": "shelf-1",
        "remote_library_id": LIBRARY,
    }
    return DestinationFacts(**{**values, **extra})  # type: ignore[arg-type]


def edition(
    edition_id: str = "ed-1", work_id: str = "w-1", title: str = "Dr Stone"
) -> EditionFacts:
    return EditionFacts(edition_id=edition_id, work_id=work_id, work_title=title, lang="fr")


def unit(
    unit_id: str = "u-1", edition_id: str = "ed-1", number: str = "1", **extra: object
) -> UnitFacts:
    values: dict[str, object] = {
        "unit_id": unit_id,
        "edition_id": edition_id,
        "number_norm": number,
        "content_hash": "sha256:" + "a" * 64,
    }
    return UnitFacts(**{**values, **extra})  # type: ignore[arg-type]


def request(**extra: object) -> PlanRequest:
    values: dict[str, object] = {
        "destination": destination(),
        "editions": (edition(),),
        "units": (unit(),),
    }
    return PlanRequest(**{**values, **extra})  # type: ignore[arg-type]


@pytest.mark.req("EF-EXP-002")
def test_m1a_shelf_below_18_refused() -> None:
    """D1: only a shelf whose ceiling is 18+ (or X, or none) exists in M1a."""
    for ceiling in (None, 18, 99):
        check_shelf_ceiling(ceiling)
    for ceiling in (0, 10, 12, 16, 17):
        with pytest.raises(ShelfBelowEighteenError) as raised:
            check_shelf_ceiling(ceiling)
        assert raised.value.code == "shelf.parental_control_unavailable"


@pytest.mark.req("EF-EXP-058")
def test_watermarked_never_planned_to_external_egress() -> None:
    watermarked = request(
        destination=destination(transport="dropbox", egress="externe"),
        units=(unit("u-1", watermarked=True), unit("u-2", number="2")),
    )
    plan = plan_destination(watermarked, simulate_parse)
    blocked = {b.unit_id: b.code for b in plan.blocked}
    assert blocked == {"u-1": "export.watermarked_external_egress"}
    assert [op.unit_id for op in plan.ops] == ["u-2"]  # the rest of the plan goes on
    at_home = request(units=(unit("u-1", watermarked=True),))
    assert [op.unit_id for op in plan_destination(at_home, simulate_parse).ops] == ["u-1"]


@pytest.mark.req("EF-EXP-058")
def test_egress_attested_per_instance_overrides_plugin_default() -> None:
    """The plugin proposes `foyer`; the admin attested `externe` for this instance: it wins."""
    assert resolve_egress(plugin_default="foyer", attested="externe") == "externe"
    assert resolve_egress(plugin_default="externe", attested="foyer") == "foyer"
    assert resolve_egress(plugin_default="foyer", attested=None) == "foyer"
    egress = resolve_egress(plugin_default="foyer", attested="externe")
    plan = plan_destination(
        request(destination=destination(egress=egress), units=(unit(watermarked=True),)),
        simulate_parse,
    )
    assert [b.code for b in plan.blocked] == ["export.watermarked_external_egress"]
    assert plan.ops == ()


@pytest.mark.req("EF-EXP-058")
def test_pull_transport_skips_reconcile_and_sentinel() -> None:
    tree = plan_destination(request(), simulate_parse)
    assert set(tree.checks) == {"sentinel", "reconcile", "foreign_writer"}
    pull = plan_destination(request(destination=destination(transport="pull")), simulate_parse)
    assert pull.checks == ()  # nothing is verified on disk: nothing is written there
    assert [(op.kind, op.rel_path) for op in pull.ops] == [("register", None)]


@pytest.mark.req("EF-EXP-058")
def test_pull_binding_requires_readable_copy() -> None:
    pull = BindingFacts("d-pull", "shelf-1", "t-opds", "pull", "foyer", None)
    with pytest.raises(PullNeedsReadableCopyError) as raised:
        check_binding(pull, [], retention_mode="passthrough")
    assert raised.value.code == "target.pull_needs_readable_copy"
    check_binding(pull, [], retention_mode="keep")  # a kept copy serves the bytes
    colinked = BindingFacts("d-tree", "shelf-1", "t-kavita", "tree", "foyer", "/library/manga")
    check_binding(pull, [colinked], retention_mode="passthrough")  # a co-linked tree does
    other_shelf = BindingFacts("d-x", "shelf-2", "t-kavita", "tree", "foyer", "/library/manga")
    with pytest.raises(PullNeedsReadableCopyError):
        check_binding(pull, [other_shelf], retention_mode="passthrough")


@pytest.mark.req("EF-EXP-058")
def test_overlapping_tree_bindings_refused() -> None:
    existing = BindingFacts("d-1", "shelf-1", "t-kavita", "tree", "foyer", "/library/manga/Kavita")
    for root in ("/library/manga/Kavita", "/library/manga/Kavita/Sub", "/library/manga"):
        new = BindingFacts("d-2", "shelf-2", "t-komga", "tree", "foyer", root)
        with pytest.raises(TreeOverlapError) as raised:
            check_binding(new, [existing], retention_mode="keep")
        assert raised.value.code == "target.tree_overlap"
    sibling = BindingFacts("d-2", "shelf-2", "t-komga", "tree", "foyer", "/library/manga/Other")
    check_binding(sibling, [existing], retention_mode="keep")  # prefix of a name is not a parent
    prefix = BindingFacts("d-3", "shelf-2", "t-komga", "tree", "foyer", "/library/manga/KavitaX")
    check_binding(prefix, [existing], retention_mode="keep")


@pytest.mark.req("EF-EXP-062")
def test_two_shelves_one_remote_library_collision_detected() -> None:
    """Shelf A already exports a "Monster" into the remote library; shelf B plans another one."""
    already = SeriesFacts(
        export_series_id="s-a",
        remote_library_id=LIBRARY,
        name_raw="Monster",
        normalized_name="monster",
        work_ids=frozenset({"w-a"}),
        edition_ids=frozenset({"ed-a"}),
    )
    other_shelf = request(
        destination=destination(shelf_id="shelf-b", shelf_destination_id="dest-b"),
        editions=(edition("ed-b", "w-b", "MONSTER"),),
        units=(unit("u-b", "ed-b"),),
        series=(already,),
        library_ceilings=frozenset({18, 99}),
    )
    plan = plan_destination(other_shelf, simulate_parse)
    assert [(b.unit_id, b.code) for b in plan.blocked] == [("u-b", "export.series_name_collision")]
    assert plan.ops == ()
    assert "age_tier_mix" in {w.code for w in plan.warnings}  # tiers mixed in one library


@pytest.mark.req("EF-EXP-062")
def test_same_work_editions_in_one_library_get_a_language_suffix() -> None:
    fr = edition("ed-fr", "w-1", "Monster")
    en = EditionFacts("ed-en", "w-1", "Monster", "en")
    plan = plan_destination(
        request(editions=(fr, en), units=(unit("u-fr", "ed-fr"), unit("u-en", "ed-en"))),
        simulate_parse,
    )
    paths = sorted(op.rel_path for op in plan.ops if op.rel_path)
    assert paths == ["Monster (en)/Monster (en) - Ch.1.cbz", "Monster (fr)/Monster (fr) - Ch.1.cbz"]


@pytest.mark.req("EF-EXP-032")
def test_series_name_collision_detected_at_plan_time() -> None:
    """Two different Works called "Monster" would be one Kavita series: the plan blocks both.

    Reduced to the block (coupable): the unit carries the generic suggestion to define an exported
    series name, not yet a ready-made "Monster (2004)".
    """
    plan = plan_destination(
        request(
            editions=(edition("ed-1", "w-1", "Monster"), edition("ed-2", "w-2", "monster")),
            units=(unit("u-1", "ed-1"), unit("u-2", "ed-2")),
        ),
        simulate_parse,
    )
    assert plan.ops == ()
    assert {(b.unit_id, b.code) for b in plan.blocked} == {
        ("u-1", "export.series_name_collision"),
        ("u-2", "export.series_name_collision"),
    }
    assert all(b.suggestion and "series name" in b.suggestion for b in plan.blocked)
    only_one = plan_destination(
        request(editions=(edition("ed-1", "w-1", "Monster"),), units=(unit("u-1", "ed-1"),)),
        simulate_parse,
    )
    assert [op.series.name_raw for op in only_one.ops] == ["Monster"]  # no clash, no block
