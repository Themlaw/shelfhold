"""Nightly against the official Kavita image (ENF-COMP-001: minimum, pinned and latest versions).

Not required to merge a pull request: `.github/workflows/nightly.yml` runs it, one job per
version, with `KAVITA_IMAGE` set. No real content source: every file is built by the test.
Run by hand: `KAVITA_IMAGE=<image> uv run pytest -m nightly tests/nightly/test_kavita_nightly.py`.
"""

from __future__ import annotations

import json
import time
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
    assert loose["volumes"] == []  # a loose chapter: no volume, whatever the title says
    placed = kavita.detail(by_name["Placement Volume"].id)
    assert any(v["minNumber"] == 3 for v in placed["volumes"]), placed["volumes"]


@pytest.mark.req("EF-EXP-009")
@pytest.mark.parametrize("kind", list(LibraryType), ids=lambda kind: kind.name)
def test_library_creation_is_accepted_for_every_library_type(
    kavita: RealKavita, kind: LibraryType
) -> None:
    """The DTO Shelfhold sends is valid for each of Kavita's six library types (the provider
    rule differs per type: a 500 `invalid-metadata-provider` otherwise)."""
    folder = f"types/{kind.name.lower()}"
    kavita.write(f"{folder}/.keep.txt", b"")
    library = kavita.create_library(f"Type {kind.name}", [kavita.remote(folder)], library_type=kind)
    assert any(lib["id"] == library for lib in run(kavita.client.list_libraries()))  # type: ignore[union-attr]


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
    # a library created without it (an adopted one) is updated by Shelfhold's faithful update;
    # what the pattern keeps out is what is written afterwards (it does not purge what is indexed)
    kavita.write("late/Late Visible/Late Visible - Ch.1.cbz", cbz(comicinfo("Late Visible", "1")))
    late = kavita.create_library("Late", [kavita.remote("late")], patterns=())
    kavita.wait_series(late, lambda s: {"Late Visible"} <= {x.name for x in s}, "Late Visible")
    update = run(kavita.client.ensure_exclude_patterns(late, HIDDEN_DIR_PATTERNS, confirmed=True))  # type: ignore[union-attr]
    assert update.changed
    kavita.write(
        "late/.shelfhold/manifest/Late Hidden - Ch.1.cbz", cbz(comicinfo("Late Hidden", "1"))
    )
    kavita.write("late/Late Visible/Late Visible - Ch.2.cbz", cbz(comicinfo("Late Visible", "2")))
    kavita.scan(kavita.remote("late"))
    time.sleep(20)
    assert {s.name for s in kavita.series(late)} == {"Late Visible"}


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


@pytest.mark.req("EF-EXP-055", "EF-BIB-034", "EF-EXP-046", "ENF-COMP-001")
def test_nightly_real_kavita_family_library_age_restriction(kavita: RealKavita) -> None:
    """A family library read by restricted accounts, on the real server (to confirm at the
    nightly, M1b): the age Shelfhold pushes is what the restriction is compared with (the ceiling
    included), a series left without an age is shown only to an account that includes unknowns,
    and the audit reads each account's restriction as Kavita holds it."""
    ages = {"Family Kids": 5, "Family Teen": 8, "Family Adult": 13}  # AgeRating: 10+, Teen, 18+
    for name in (*ages, "Family Unrated"):
        kavita.write(f"family/{name}/{name} - Ch.1.cbz", cbz(comicinfo(name, "1")))
    library = kavita.create_library("Family", [kavita.remote("family")])
    kavita.scan(kavita.remote("family"))
    found = kavita.wait_series(library, lambda s: len(s) == 4, "the four series")
    for series in found:  # what Shelfhold does after the export: the owned, locked age
        if series.name in ages:
            current = run(kavita.client.series_age(series.id))  # type: ignore[union-attr]
            run(kavita.client.set_series_age(current, ages[series.name]))  # type: ignore[union-attr]
    everything = {s.name for s in found}

    teen = kavita.restricted_account("teen", [library], age_rating=8, include_unknowns=False)
    assert kavita.visible_to(teen) == {"Family Kids", "Family Teen"}  # the ceiling is included
    tolerant = kavita.restricted_account("tolerant", [library], age_rating=8, include_unknowns=True)
    assert kavita.visible_to(tolerant) == {"Family Kids", "Family Teen", "Family Unrated"}
    small = kavita.restricted_account("small", [library], age_rating=5, include_unknowns=False)
    assert kavita.visible_to(small) == {"Family Kids"}
    free = kavita.restricted_account("free", [library], age_rating=-1, include_unknowns=True)
    assert kavita.visible_to(free) == everything  # no restriction: the adult series is read

    audited = {m.username: m for m in run(kavita.client.list_users())}  # type: ignore[union-attr]
    assert (audited["teen"].age_rating, audited["teen"].include_unknowns) == (8, False)
    assert (audited["tolerant"].age_rating, audited["tolerant"].include_unknowns) == (8, True)
    assert audited["free"].age_rating == -1 and audited["free"].include_unknowns
    assert all(audited[name].library_ids == {library} for name in ("teen", "tolerant", "small"))


def _epub(title: str) -> bytes:
    """A well-formed EPUB with its own identifier and title, no age anywhere in it (L09: the age
    of an EPUB series is never written in the file)."""
    import hashlib

    from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, opf, xhtml

    ident = hashlib.sha256(title.encode()).hexdigest()[:32]
    package = (
        opf()
        .replace(b"urn:uuid:00000000-0000-0000-0000-000000000001", f"urn:uuid:{ident}".encode())
        .replace(b"<dc:title>Synthetic</dc:title>", f"<dc:title>{title}</dc:title>".encode())
    )
    return epub_bytes(xhtml(f"<p>{title}</p>"), opf_bytes=package)


@pytest.mark.req("EF-EXP-033", "EF-EXP-040", "ENF-COMP-001")
def test_nightly_real_kavita_epub_series_age_lock(kavita: RealKavita) -> None:
    """DoD 03 on the real server, for each version: the age of an EPUB series is pushed through
    the API and locked, and the lock survives the scans that follow (a Books library reads no age
    in the file, so nothing else could set it back). A restricted account then reads the series
    its ceiling allows and no other, the series without an age only when it includes unknowns."""
    ages = {"Book Kids": 5, "Book Adult": 13}  # AgeRating: 10+ and 18+
    for name in (*ages, "Book Unrated"):
        kavita.write(f"books/{name}/{name}.epub", _epub(name))
    library = kavita.create_library(
        "Books", [kavita.remote("books")], library_type=LibraryType.BOOK
    )
    kavita.scan(kavita.remote("books"))
    found = kavita.wait_series(library, lambda s: len(s) == 3, "the three book series")
    for series in found:
        if series.name in ages:
            current = run(kavita.client.series_age(series.id))  # type: ignore[union-attr]
            assert not current.locked  # nothing was locked before Shelfhold pushed
            run(kavita.client.set_series_age(current, ages[series.name]))  # type: ignore[union-attr]

    kavita.scan(kavita.remote("books"))  # the lock is checked after a scan, not before
    kavita.settle(library, 10)
    for series in kavita.series(library):
        age = run(kavita.client.series_age(series.id))  # type: ignore[union-attr]
        if series.name in ages:
            assert (age.age_rating, age.locked) == (ages[series.name], True), series.name
        else:
            assert not age.locked  # the series left without an age stays "unknown"

    kids = kavita.restricted_account("kid", [library], age_rating=5, include_unknowns=False)
    assert kavita.visible_to(kids) == {"Book Kids"}
    tolerant = kavita.restricted_account("tolerant", [library], age_rating=5, include_unknowns=True)
    assert kavita.visible_to(tolerant) == {"Book Kids", "Book Unrated"}
    free = kavita.restricted_account("free", [library], age_rating=-1, include_unknowns=True)
    assert kavita.visible_to(free) == {"Book Kids", "Book Adult", "Book Unrated"}
