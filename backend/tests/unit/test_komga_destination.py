"""EF-EXP-020, EF-EXP-032, ENF-QUAL-003 (M4 L08a): the Komga destination, as a describer.

Komga reads the **folder** as the series, Kavita reads the **name**; the common render of ADR-0052
makes the two coincide (`<root>/<Series>/<Series> - …`, two levels, series as leaves). The
destination declares what it is (an indexer of a tree it did not make, with no folder scan, that
keeps accounts apart for nobody yet: L08b raises that with its audit) and describes calls the
core runs. It writes nothing and holds no secret.
"""

from __future__ import annotations

import pytest

from shelfhold.export.targets.kavita import KavitaDestination
from shelfhold.export.targets.komga import KomgaDestination
from shelfhold.sdk.fsops import FsOpPlan
from shelfhold.sdk.testing.destination_kit import (
    DestinationScenario,
    assert_destination_conforms,
    run_destination_conformance,
)
from tests.fakes.kavita.plugin_parse import simulate_parse

LIBRARY = "0SYNTHMANGA01"
SERIES = ("Synthetic Series A", "Synthetic Series B", "Élise et l'Atelier")


def _tree(series: tuple[str, ...] = SERIES) -> list[str]:
    """The layout of the common render: a folder per series, the series name at the head of each
    file name, whatever the number of chapters."""
    return [f"{s}/{s} - Ch.{n:03d}.cbz" for s in series for n in (1, 2)]


@pytest.mark.req("EF-EXP-020", "ENF-QUAL-003", "EF-EXP-065")
def test_komga_passes_destination_kit() -> None:
    destination = KomgaDestination()
    caps = destination.capabilities
    assert (caps.transport, caps.egress, caps.readback) == ("tree", "foyer", "none")
    assert caps.can_purge is False  # `empty-trash` forgets Komga's records, the host erases files
    assert caps.indexes_tree is True and caps.path_scoped_scan is False
    assert caps.accepted_content_types == ["cbz", "epub"] and caps.name_normalizer == "komga-1"
    # declared by library since L08b; it counts only for a folder whose accounts the audit proved
    assert caps.access_partitioning == "library"
    scan, trash = destination.scan_library_call(LIBRARY), destination.forget_removed_call(LIBRARY)
    assert (scan.method, scan.path) == ("POST", f"/api/v1/libraries/{LIBRARY}/scan")
    assert (trash.method, trash.path) == ("POST", f"/api/v1/libraries/{LIBRARY}/empty-trash")
    assert scan.auth == trash.auth == "target_admin" and not scan.query and scan.body is None
    report = assert_destination_conforms(
        destination,
        DestinationScenario(key="komga", plan=FsOpPlan(ops=()), calls=(scan, trash)),
    )
    assert report.ok
    # an indexer that is given a plan that writes does not conform
    from shelfhold.sdk.fsops import FsOp

    writing = FsOpPlan(ops=(FsOp(op="mkdir", rel_path="Synthetic Series"),))
    broken = run_destination_conformance(
        destination, DestinationScenario(key="komga", plan=writing, calls=(scan,))
    )
    assert {c.name for c in broken.failures} == {"indexer_emits_no_fsop_write"}


@pytest.mark.req("EF-EXP-020")
def test_komga_describes_no_call_that_names_a_folder_or_carries_a_secret() -> None:
    destination = KomgaDestination()
    assert destination.scan_calls(["/manga/Synthetic Series A"]) == []
    inventory = destination.inventory_calls(LIBRARY)
    assert [(c.method, c.path) for c in inventory] == [("GET", "/api/v1/libraries")]
    for call in (*inventory, destination.scan_library_call(LIBRARY)):
        dumped = call.model_dump_json()
        assert "Synthetic" not in dumped and "key" not in dumped.lower().replace("target_admin", "")
    with pytest.raises(ValueError):  # a library id is an identifier, never a path
        destination.scan_library_call("../users")
    with pytest.raises(ValueError):
        destination.forget_removed_call("0SYNTH/scan")


@pytest.mark.req("EF-EXP-032")
def test_komga_folder_grouping_vs_kavita_name_grouping() -> None:
    komga, kavita = KomgaDestination, KavitaDestination
    assert komga.capabilities.series_grouping == "folder"
    assert kavita.capabilities.series_grouping == "name"  # ADR-0052: Kavita reads the name
    assert komga.capabilities.name_normalizer != kavita.capabilities.name_normalizer

    destination = KomgaDestination()
    tree = _tree()
    by_folder = destination.series_of(tree)
    assert set(by_folder) == set(SERIES)  # a folder, a series
    for folder, files in by_folder.items():
        assert len(files) == 2
        for path in files:
            # what Kavita reads in the name of the file is the folder Komga takes as the series
            assert simulate_parse(path.rsplit("/", 1)[-1]).series_name == folder
    assert destination.layout_conflicts(tree) == []

    # the same chapters under a nested layout: Kavita merges them, Komga makes a series of each
    nested = [
        "Synthetic Series A/Vol 1/Synthetic Series A - Ch.001.cbz",
        "Synthetic Series A/Vol 2/Synthetic Series A - Ch.002.cbz",
    ]
    assert set(destination.series_of(nested)) == {
        "Synthetic Series A/Vol 1", "Synthetic Series A/Vol 2",
    }  # fmt: skip
    assert {simulate_parse(p.rsplit("/", 1)[-1]).series_name for p in nested} == {
        "Synthetic Series A"
    }
    assert {c.reason for c in destination.layout_conflicts(nested)} == {"nested_series_folder"}
    # a file at the root is a one-shot series for Komga, and no folder says its name
    assert [c.reason for c in destination.layout_conflicts(["Synthetic - Ch.001.cbz"])] == [
        "file_at_library_root"
    ]
    # a folder and a file name that disagree: the render is refused, never rewritten
    odd = ["Synthetic Series A/Another Series - Ch.001.cbz"]
    assert [c.reason for c in destination.layout_conflicts(odd)] == ["name_differs_from_folder"]


@pytest.mark.req("EF-EXP-032", "EF-EXP-020")
def test_komga_declares_what_it_never_does() -> None:
    caps = KomgaDestination.capabilities
    assert caps.can_update is False and caps.manage_libraries is False
    assert (caps.events, caps.collections, caps.progress) == ("none", "none", "none")
    assert caps.sidecars == {"comicinfo"} and caps.forbidden_sidecars == frozenset()
    assert caps.confirms_arrival is False and caps.reconcilable is False
