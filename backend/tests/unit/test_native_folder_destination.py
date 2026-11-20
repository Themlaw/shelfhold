"""EF-EXP-010 (M4 L07, intouchable : intégrité): the native folder destination describes the local
layout of Mihon, read in its `LocalSource` (the base folder, a folder per series, a file per
chapter named `Ch.<number> <title>`, the cover named `cover.*`, the details in a `ComicInfo.xml`,
never a `*.json`, which Mihon takes for its legacy details file and deletes).

The destination only describes a plan: it never touches the disk. The kit of L05 replays it.
"""

import re

import pytest
from shelfhold_sdk.export_target import TargetCapabilities
from shelfhold_sdk.fsops import ContentRef, FsOp, FsOpPlan, is_reserved_path
from shelfhold_sdk.testing.destination_kit import (
    SYNTHETIC_ITEM,
    DestinationScenario,
    assert_destination_conforms,
)

from shelfhold.export.targets.native_folder.destination import (
    ChapterSpec,
    NativeFolderDestination,
    SeriesSpec,
    cover_ref,
)
from shelfhold.export.targets.native_folder.names import chapter_filename

# ChapterRecognition.kt of Mihon: `basic = (?<=ch\.) *NUMBER_PATTERN`,
# NUMBER_PATTERN = ([0-9]+)(\.[0-9]+)?
_MIHON_BASIC = re.compile(r"(?<=ch\.) *([0-9]+)(\.[0-9]+)?")
_ARCHIVES = (".cbz", ".epub")


def _series(
    *chapters: ChapterSpec, name: str = "Series Alpha", cover: str | None = "I1"
) -> SeriesSpec:
    return SeriesSpec(name=name, chapters=tuple(chapters), cover_item=cover)


def _plan(series: SeriesSpec) -> FsOpPlan:
    return NativeFolderDestination().describe([series])


@pytest.mark.req("EF-EXP-010")
def test_native_folder_mihon_local_layout() -> None:
    plan = _plan(
        _series(
            ChapterSpec("I1", "12", "Dawn", "cbz"),
            ChapterSpec("I2", "12.5", "", "cbz"),
            ChapterSpec("I3", "3", "The / Lake?", "epub"),
        )
    )
    ops = [(o.op, o.rel_path) for o in plan.ops]
    assert ops == [
        ("mkdir", "Series Alpha"),
        ("write", "Series Alpha/Ch.0003 The Lake.epub"),  # one EPUB is one chapter
        ("write", "Series Alpha/Ch.0012 Dawn.cbz"),
        ("write", "Series Alpha/Ch.0012.5.cbz"),  # no title: no trailing space
        ("write_inline", "Series Alpha/cover.jpg"),  # Mihon: a file named `cover.*`, by content
    ]
    for op in plan.ops:
        if op.op != "write":
            continue
        name = op.rel_path.split("/")[1]
        assert name.lower().endswith(_ARCHIVES) and not name.startswith(".")
        found = _MIHON_BASIC.search(name.rsplit(".", 1)[0].casefold())
        assert found is not None, name  # the number Mihon reads is the chapter's
    numbers = [
        float(_MIHON_BASIC.search(o.rel_path.split("/")[1].rsplit(".", 1)[0].casefold())[0])  # type: ignore[index]
        for o in plan.ops
        if o.op == "write"
    ]
    assert numbers == [3.0, 12.0, 12.5]
    cover = plan.ops[-1]
    assert cover.content_ref == ContentRef(kind="sidecar", ref=cover_ref("I1"))
    assert all(
        o.content_ref.kind == "export_item" for o in plan.ops if o.op == "write" and o.content_ref
    )


@pytest.mark.req("EF-EXP-010")
def test_native_folder_names_are_the_ones_mihon_reads() -> None:
    assert chapter_filename("7", "Title", "cbz") == "Ch.0007 Title.cbz"
    assert chapter_filename("100", "", "cbz") == "Ch.0100.cbz"
    assert chapter_filename("0", "Prologue", "cbz") == "Ch.0000 Prologue.cbz"
    assert chapter_filename("12.50", "", "epub") == "Ch.0012.5.epub"
    assert chapter_filename("12345", "Long", "cbz") == "Ch.12345 Long.cbz"  # four digits at least
    assert chapter_filename(None, "Side story", "cbz") == "Side story.cbz"
    # a title never makes a path, a hidden file or a second extension of the series folder
    for hostile in ("../../x", "a/b\\c", ".hidden", "con\u0000trol", "x" * 400):
        name = chapter_filename("1", hostile, "cbz")
        assert "/" not in name and "\\" not in name and "\u0000" not in name
        assert not name.startswith(".") and len(name.encode()) <= 255 and name.endswith(".cbz")


@pytest.mark.req("EF-EXP-010")
def test_native_folder_no_json_in_series_dirs() -> None:
    caps = NativeFolderDestination.capabilities
    assert {"series.json", "details.json", "metadata.json"} <= set(caps.forbidden_sidecars)
    assert "metadata.json" not in caps.sidecars and "series.json" not in caps.sidecars
    plan = _plan(
        _series(
            ChapterSpec("I1", "1", "details.json", "cbz"),
            ChapterSpec("I2", "2", "series", "cbz"),
        )
    )
    for op in plan.ops:
        assert not op.rel_path.casefold().endswith(".json"), op
        assert op.dst_rel_path is None
    # a series named like a legacy file is still a folder with archives inside, never a file
    assert all(o.rel_path.startswith("Series Alpha") for o in plan.ops)


@pytest.mark.req("EF-EXP-010")
def test_native_folder_declares_what_the_card_says() -> None:
    caps = NativeFolderDestination.capabilities
    assert isinstance(caps, TargetCapabilities)
    assert caps.transport == "tree" and caps.access_partitioning == "none"
    assert caps.series_grouping == "folder" and caps.egress == "foyer"
    assert caps.sidecars == {"comicinfo", "cover-file"}
    assert caps.accepted_content_types == ["cbz", "epub"]  # no RAR5, no PDF
    assert caps.formats == {"cbz", "epub"} and not caps.can_purge
    assert not caps.indexes_tree and caps.events == "none" and caps.progress == "none"


@pytest.mark.req("EF-EXP-010")
def test_native_folder_never_names_the_host_area_or_two_chapters_alike() -> None:
    plan = _plan(
        _series(
            ChapterSpec("I1", "1", "Same", "cbz"),
            ChapterSpec("I2", "1", "Same", "cbz"),  # the same number and title: one file each
            name=".shelfhold-x",
        )
    )
    paths = [o.rel_path for o in plan.ops]
    assert len({p.casefold() for p in paths}) == len(paths)
    assert not any(is_reserved_path(p) for p in paths)  # the series folder is renamed, not refused


@pytest.mark.req("EF-EXP-010", "ENF-QUAL-003")
def test_native_folder_passes_destination_kit() -> None:
    destination = NativeFolderDestination()
    plan = FsOpPlan(
        ops=(
            FsOp(op="mkdir", rel_path="Synthetic Series"),
            FsOp(
                op="write",
                rel_path="Synthetic Series/Ch.0001 One.cbz",
                content_ref=ContentRef(kind="export_item", ref=SYNTHETIC_ITEM),
            ),
        )
    )
    report = assert_destination_conforms(
        destination, DestinationScenario(key="native_folder", plan=plan)
    )
    assert report.ok
    # what the destination itself describes for the synthetic item passes the same replay
    described = destination.describe(
        [
            _series(
                ChapterSpec(SYNTHETIC_ITEM, "1", "One", "cbz"), cover=None, name="Synthetic Series"
            )
        ]
    )
    assert_destination_conforms(
        destination, DestinationScenario(key="native_folder", plan=described)
    )
