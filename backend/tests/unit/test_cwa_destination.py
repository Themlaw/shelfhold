"""EF-EXP-022, EF-EXP-047, ENF-QUAL-003 (M4 L09): the CWA drop box, as a describer.

A drop box is a folder an ingester watches: the destination says what it is (one transport,
`dropbox`; an EPUB at a time; no tree, no sentinel, no other-writer detection, no accounts kept
apart) and describes the plan that drops files; it writes nothing and holds no secret. The kit of
L05 is replayed in full on it, and each rule of the kit that concerns a drop box is also broken
on purpose to show that the kit would say so.
"""

import pytest
from shelfhold_sdk.fsops import ContentRef, FsOp, FsOpPlan
from shelfhold_sdk.testing.destination_kit import (
    SYNTHETIC_ITEM,
    SYNTHETIC_WATERMARKED,
    DestinationScenario,
    assert_destination_conforms,
    run_destination_conformance,
)

from shelfhold.export.domain.bindings import effective_partitioning
from shelfhold.export.targets.cwa import CwaDestination, DropSpec

ITEM = "01HZSYNTHBOOK00000000000A"
OTHER = "01HZSYNTHBOOK00000000000B"


@pytest.mark.req("EF-EXP-022", "ENF-QUAL-003", "EF-EXP-065")
def test_cwa_passes_destination_kit() -> None:
    destination = CwaDestination()
    plan = destination.describe([DropSpec(SYNTHETIC_ITEM, "Synthetic Book A")])
    assert [op.op for op in plan.ops] == ["write"]
    report = assert_destination_conforms(destination, DestinationScenario(key="cwa", plan=plan))
    assert report.ok
    # the kit replayed in full: every check it has ran, and none is skipped for a drop box
    assert {"transport_declared", "egress_declared", "plan_roundtrips_json"} <= {
        c.name for c in report.checks
    }
    # an egress outside the home never takes a watermarked file: the same plan, refused
    mark = destination.describe([DropSpec(SYNTHETIC_WATERMARKED, "Synthetic Book B")])
    external = type(
        "External",
        (),
        {"capabilities": destination.capabilities.model_copy(update={"egress": "externe"})},
    )()
    broken = run_destination_conformance(external, DestinationScenario(key="cwa", plan=mark))
    assert {c.name for c in broken.failures} == {"plan_checked_before_run"}
    # the plan of a drop box that writes more than a file at the root of its folder is not one
    nested = FsOpPlan(ops=(FsOp(op="mkdir", rel_path="Series"),))
    assert run_destination_conformance(
        destination, DestinationScenario(key="cwa", plan=nested)
    ).ok  # the kit has no rule for it: the host's executor refuses it (see the executor test)


@pytest.mark.req("EF-EXP-022", "EF-EXP-047", "EF-EXP-058")
def test_cwa_declares_what_it_is_and_what_it_never_does() -> None:
    caps = CwaDestination.capabilities
    assert (caps.transport, caps.egress, caps.readback) == ("dropbox", "foyer", "none")
    assert caps.access_partitioning == "none"  # so: no shelf below 18+ (EF-EXP-047)
    assert caps.accepted_content_types == ["epub"] and caps.formats == {"epub"}
    # no tree, no sentinel, no other-writer detection: none of it is declared (EF-EXP-058)
    assert caps.indexes_tree is False and caps.reconcilable is False
    assert caps.confirms_arrival is False and caps.path_scoped_scan is False
    assert caps.can_update is False and caps.can_delete is False and caps.can_purge is False
    assert (caps.events, caps.collections, caps.progress) == ("none", "none", "none")
    assert caps.sidecars == set() and caps.forbidden_sidecars == frozenset()
    assert caps.manage_libraries is False and caps.content_body is False
    destination = CwaDestination()
    assert destination.scan_calls(["/ingest"]) == [] and destination.inventory_calls("x") == []
    assert destination.render_sidecars({}, {}) == []


@pytest.mark.req("EF-EXP-047", "EF-EXP-065")
@pytest.mark.parametrize("attested", [True, False])
def test_what_an_admin_attests_never_makes_a_drop_box_partitioned(attested: bool) -> None:
    for stored in ("none", "library", "user"):
        assert effective_partitioning("cwa", stored, attested=attested) == "none"


@pytest.mark.req("EF-EXP-022")
def test_the_name_of_a_drop_is_one_safe_component_that_carries_the_item() -> None:
    destination = CwaDestination()
    titles = [
        "Synthetic Book A",
        "../../etc/passwd",
        'a/b\\c:d*e?f"g<h>i|j',
        ".hidden",
        "",
        "   ",
        "\x00\x1f control ‮ bidi",
        "x" * 400,
        "Élise et l'Atelier",
    ]
    ids = [f"item{n}" for n in range(len(titles))]
    plan = destination.describe([DropSpec(i, t) for i, t in zip(ids, titles, strict=True)])
    assert len(plan.ops) == len(titles)
    for op, item in zip(plan.ops, ids, strict=True):
        assert op.op == "write" and op.content_ref == ContentRef(kind="export_item", ref=item)
        name = op.rel_path
        assert "/" not in name and "\\" not in name and "\0" not in name
        assert not name.startswith(".") and name.endswith(f"[{item}].epub")
        assert len(name.encode()) <= 255
    assert plan.ops[0].rel_path == "Synthetic Book A [item0].epub"
    # two items with one title never share a name
    pair = destination.describe([DropSpec(ITEM, "Same"), DropSpec(OTHER, "Same")])
    assert pair.ops[0].rel_path != pair.ops[1].rel_path


@pytest.mark.req("EF-EXP-022")
def test_an_item_id_that_is_not_an_identifier_is_refused() -> None:
    destination = CwaDestination()
    for bad in ("", "../x", "a/b", "x" * 80, "id with space", "é"):
        with pytest.raises(ValueError):
            destination.describe([DropSpec(bad, "Title")])
