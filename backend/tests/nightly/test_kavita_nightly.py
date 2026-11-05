"""Nightly against the official Kavita image (ENF-COMP-001: minimum, pinned and latest versions).

Not required to merge a pull request: `.github/workflows/nightly.yml` runs it, one job per
version, with `KAVITA_IMAGE` set. No real content source: every file is built by the test.
Run by hand: `KAVITA_IMAGE=<image> uv run pytest -m nightly tests/nightly/test_kavita_nightly.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS, LibraryType

from .kavita_real import RealKavita, cbz, comicinfo, run

pytestmark = pytest.mark.nightly
SEGMENTS = Path(__file__).resolve().parents[1] / "golden" / "kavita_naming" / "segments.json"
MIRROR_LINE = json.dumps({"format": "1.2", "unit": "synthetic", "note": "provenance mirror line"})


@pytest.mark.req("ENF-COMP-001", "EF-EXP-045")
def test_nightly_real_kavita_places_chapter_volume_from_comicinfo(kavita: RealKavita) -> None:
    """A file named `Series - Ch.N` is a chapter, whatever its ComicInfo says of the title; the
    volume is the one ComicInfo declares, and the title never becomes a volume (EF-EXP-045)."""
    kavita.write(
        "placement/Placement Loose/Placement Loose - Ch.1.cbz",
        cbz(comicinfo("Placement Loose", "1", title="Chapter 1 - S1 END")),
    )
    kavita.write(
        "placement/Placement Volume/Placement Volume - Ch.1.cbz",
        cbz(comicinfo("Placement Volume", "1", volume="3")),
    )
    library = kavita.create_library("Placement", [kavita.remote("placement")])
    kavita.scan(kavita.remote("placement"))
    found = kavita.wait_series(library, lambda s: len(s) >= 2, "two series")
    by_name = {s.name: s for s in found}
    assert set(by_name) >= {"Placement Loose", "Placement Volume"}
    loose = kavita.detail(by_name["Placement Loose"].id)
    assert [c["files"][0]["filePath"].endswith("Ch.1.cbz") for c in loose["chapters"]] == [True]
    assert loose["volumes"] and all(v["minNumber"] < 0 for v in loose["volumes"]), loose["volumes"]
    placed = kavita.detail(by_name["Placement Volume"].id)
    assert any(v["minNumber"] == 3 for v in placed["volumes"]), placed["volumes"]


@pytest.mark.req("ENF-COMP-001", "EF-EXP-045", "EF-BIB-050")
def test_segment_naming_against_real_kavita_nightly(kavita: RealKavita) -> None:
    """Q50: the names of `golden/kavita_naming/segments.json` read by a real `plugin/parse` and
    indexed by a real scan give the series, chapter and volume the golden file records. Its
    success is what allows `naming_segment_policy_confirmed` to become true (L24)."""
    cases = json.loads(SEGMENTS.read_text("utf-8"))
    assert cases, "the golden file is empty"
    for case in cases:
        name = case["rel_path"].rsplit("/", 1)[-1]
        parsed = run(kavita.client.parse_name(name, LibraryType.MANGA))  # type: ignore[union-attr]
        assert parsed.series_name == case["series"], (name, parsed)
        assert parsed.min_chapter == case["chapter"], (name, parsed)
        assert parsed.min_volume == case["volume"], (name, parsed)
        kavita.write(
            f"segments/{case['rel_path']}", cbz(comicinfo(case["series"], str(case["chapter"])))
        )
    library = kavita.create_library("Segments", [kavita.remote("segments")])
    kavita.scan(kavita.remote("segments"))
    wanted = {case["series"] for case in cases}
    found = kavita.wait_series(library, lambda s: wanted <= {x.name for x in s}, "every segment")
    assert wanted <= {s.name for s in found}  # a season segment is a series of its own, as named


def _library_with_reserved_area(
    kavita: RealKavita, tag: str, *, roots: int
) -> tuple[int, list[str]]:
    """`roots` physical roots, each with a visible series and a reserved area holding a mirror
    line and a CBZ that would be a series if Kavita looked at it."""
    folders: list[str] = []
    for index in range(roots):
        root = f"{tag}/root{index}"
        kavita.write(f"{root}/Visible {tag} {index}/Visible {tag} {index} - Ch.1.cbz",
                     cbz(comicinfo(f"Visible {tag} {index}", "1")))  # fmt: skip
        kavita.write(f"{root}/.shelfhold/manifest/units-{index}.jsonl", MIRROR_LINE.encode())
        kavita.write(f"{root}/.shelfhold/manifest/Hidden {tag} {index} - Ch.1.cbz",
                     cbz(comicinfo(f"Hidden {tag} {index}", "1")))  # fmt: skip
        folders.append(kavita.remote(root))
    library = kavita.create_library(f"Reserved {tag}", folders, patterns=HIDDEN_DIR_PATTERNS)
    for folder in folders:
        kavita.scan(folder)
    return library, folders


def _assert_only_visible(kavita: RealKavita, library: int, tag: str, roots: int) -> None:
    wanted = {f"Visible {tag} {i}" for i in range(roots)}  # the positive control: a scan did run
    kavita.wait_series(library, lambda s: wanted <= {x.name for x in s}, "the visible series")
    settled = kavita.settle(library)
    assert {s.name for s in settled} == wanted, [s.name for s in settled]
    for series in settled:
        files = [c["files"][0]["filePath"] for c in kavita.chapters(series.id)]
        assert not any("/.shelfhold/" in f for f in files), files


@pytest.mark.req("ENF-COMP-001", "EF-EXP-059")
def test_kavita_never_indexes_shelfhold_dir_per_root(kavita: RealKavita) -> None:
    library, _ = _library_with_reserved_area(kavita, "perroot", roots=2)
    _assert_only_visible(kavita, library, "perroot", 2)


@pytest.mark.req("ENF-COMP-001", "EF-EXP-063")
def test_manifest_mirror_dir_never_indexed_by_kavita(kavita: RealKavita) -> None:
    library, _ = _library_with_reserved_area(kavita, "manifest", roots=1)
    _assert_only_visible(kavita, library, "manifest", 1)


@pytest.mark.req("ENF-COMP-001", "EF-EXP-063")
def test_mirror_never_indexed_by_kavita_nightly(kavita: RealKavita) -> None:
    """The pattern is valid Kavita syntax (GAP-6-26): the library keeps it as written, an update
    that adds it to an existing library is faithful, and the mirror stays invisible after both."""
    library, _ = _library_with_reserved_area(kavita, "pattern", roots=1)
    kept = next(lib for lib in run(kavita.client.list_libraries()) if lib["id"] == library)  # type: ignore[union-attr]
    assert list(HIDDEN_DIR_PATTERNS) == [
        p for p in kept["excludePatterns"] if p in HIDDEN_DIR_PATTERNS
    ]
    _assert_only_visible(kavita, library, "pattern", 1)
    # a library created without it (an adopted one) is updated by Shelfhold's faithful update
    kavita.write(
        "late/.shelfhold/manifest/Late Hidden - Ch.1.cbz", cbz(comicinfo("Late Hidden", "1"))
    )
    kavita.write("late/Late Visible/Late Visible - Ch.1.cbz", cbz(comicinfo("Late Visible", "1")))
    late = kavita.create_library("Late", [kavita.remote("late")], patterns=())
    run(kavita.client.ensure_exclude_patterns(late, HIDDEN_DIR_PATTERNS, confirmed=True))  # type: ignore[union-attr]
    kavita.scan(kavita.remote("late"))
    kavita.wait_series(late, lambda s: {"Late Visible"} <= {x.name for x in s}, "Late Visible")
    assert {s.name for s in kavita.settle(late)} == {"Late Visible"}


@pytest.mark.req("EF-EXP-051", "EF-EXP-052")
def test_repatriate_keeps_kavita_progress_nightly(kavita: RealKavita) -> None:
    """After a repatriation and the unmount of the old mount, the files being the same bytes at the
    same paths: same series, same identifier, reading progress intact (the simulated Kavita
    does not model progress)."""
    relative = "repatriate/Repatriated/Repatriated - Ch.1.cbz"
    content = cbz(comicinfo("Repatriated", "1"), pages=5)
    path = kavita.write(relative, content)
    library = kavita.create_library("Repatriate", [kavita.remote("repatriate")])
    kavita.scan(kavita.remote("repatriate"))
    (series,) = kavita.wait_series(library, lambda s: len(s) == 1, "the series")
    (chapter,) = kavita.chapters(series.id)
    kavita.set_progress(library, series.id, chapter, 3)
    assert kavita.progress(chapter["id"]) == 3
    # the old mount goes, the kept copy answers at the same path with the same bytes
    path.unlink()
    kavita.write(relative, content)
    kavita.scan(kavita.remote("repatriate/Repatriated"))
    kavita.settle(library, 10)
    (after,) = kavita.series(library)
    assert (after.id, after.name) == (series.id, series.name)
    (same,) = kavita.chapters(after.id)
    assert same["id"] == chapter["id"]
    assert kavita.progress(same["id"]) == 3
