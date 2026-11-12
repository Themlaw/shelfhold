"""M2 L21: import an existing Suwayomi download tree without downloading it again (EF-INT-009).

Real files, a real database, the real stores. The tree is somebody else's: every guard of the
import folder holds (no link, no special file, no trapped name, nothing half-written), the images
of a folder meet the same allowlist as any CBZ, a purged content or series stays purged, a chapter
already stored is not stored twice, and nothing is ever removed from or written into the tree.
"""

import hashlib
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import TreeTargets, chapter_number
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.library.adapters.import_scan import make_precheck
from shelfhold.plugin_host.adapters.meta.suwayomi import tree_import
from shelfhold.plugin_host.adapters.meta.suwayomi.tree_import import SuwayomiTree, TreeReport
from tests.import_env import OLD, ImportEnv, import_env
from tests.library_env import JPEG, PNG, cbz_bytes
from tests.null_tombstones import NONE_PURGED

SERIES = "Un titre"  # the Work of the bench
CH = f"mangas/Source (EN)/{SERIES}"
CBZ = cbz_bytes()
PAGE_A = PNG + b"a"
PAGE_B = JPEG + b"b"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def snapshot(root: Path) -> list[tuple[str, int, int]]:
    """Every name under the tree with its size and time: what 'untouched' means."""
    found = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        found.append((str(path.relative_to(root)), info.st_size, info.st_mtime_ns))
    return found


class Gate:
    """The tombstone gate: a series and/or a content can be purged."""

    def __init__(self, *, series: bool = False, content: bool = False) -> None:
        self.series, self.content = series, content

    def check(self, candidate: object, *, root_id: str | None = None, **_: Any) -> Any:
        return SimpleNamespace(blocked=self.series)

    def check_content(self, hashes: object = (), **_: Any) -> bool:
        return self.content


def seed_source(engine: Any, *, warning: str | None = "safe") -> None:
    """The sub-source the bench's binding belongs to: its folder is `Source (EN)`."""
    table = acquisition.sub_source
    with engine.begin() as connection:
        connection.execute(sa.delete(table))
        connection.execute(
            sa.insert(table).values(
                plugin_id="suwayomi",
                source_key="suwayomi:1",
                name="Source",
                lang="en",
                content_warning=warning,
            )
        )


def tree_of(env: ImportEnv, gate: Any = NONE_PURGED, **kw: Any) -> SuwayomiTree:
    lib = env.lib

    def place(unit_id: str, variant_id: str, extension: str) -> str:
        return str(lib.manga / "Imported" / f"{variant_id}{extension}")

    options: dict[str, Any] = {
        "engine": lib.engine, "fsops": lib.fsops, "store": env.router,
        "staging_dir": lib.tmp / "staging", "place": place,
        "keep_apart": (lib.library, lib.store, lib.data), "sleep": lambda _s: None,
        "precheck": make_precheck(gate),
    }  # fmt: skip
    options.update(kw)
    seed_source(lib.engine)
    return SuwayomiTree(
        env.incoming, targets=TreeTargets(lib.engine, SystemClock(), gate), **options
    )


def page_folder(env: ImportEnv, chapter: str, pages: dict[str, bytes], *, age: float = OLD) -> Path:
    folder = env.incoming / CH / chapter
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in pages.items():
        env.drop(f"{CH}/{chapter}/{name}", data, age=age)
    return folder


def states(report: TreeReport) -> list[tuple[str, str | None]]:
    return [(o.state, o.code) for o in report.outcomes]


def refusal_codes(report: TreeReport) -> list[str]:
    return [v.code for v in report.refused] + [o.code or "" for o in report.outcomes]


# -- the happy road ----------------------------------------------------------------------


@pytest.mark.req("EF-INT-009")
def test_import_existing_suwayomi_download_tree() -> None:
    """A CBZ and a folder of images become Units of the existing binding, nothing is fetched,
    nothing in the tree is touched, and a chapter the follow already knew gets its file."""
    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", CBZ)  # the bench's Unit 1 is known and has no file
        page_folder(env, "Chapter 2", {"002.jpg": PAGE_B, "001.png": PAGE_A})
        before = snapshot(env.incoming)
        report = tree_of(env).import_tree()
        assert states(report) == [("stored", None), ("stored", None)]
        assert snapshot(env.incoming) == before  # nothing removed, nothing written
        assert not (env.incoming / ".shelfhold-removing").exists()
        contents = env.contents()
        assert len(contents) == 2
        assert {c.unit_id for c in contents} >= {env.lib.chain["unit"]}  # filled, not duplicated
        with env.lib.engine.connect() as connection:
            units = connection.execute(sa.select(sa.func.count()).select_from(catalog.unit))
            assert units.scalar_one() == 2
            trust = connection.execute(sa.select(acquisition.source_binding.c.content_trust))
            assert trust.scalar_one() in ("untrusted", "publisher")
        assert all(c.format == "cbz" for c in contents)


@pytest.mark.req("EF-INT-009")
def test_dry_run_reads_and_changes_nothing() -> None:
    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", CBZ)
        before = snapshot(env.incoming)
        report = tree_of(env).import_tree(dry_run=True)
        assert report.ready == (f"{CH}/Chapter 1.cbz",) and report.outcomes == ()
        assert env.contents() == [] and snapshot(env.incoming) == before


@pytest.mark.req("EF-INT-009")
def test_the_tree_may_be_given_at_its_mangas_folder_or_above() -> None:
    with import_env() as env:
        env.drop(f"Source (EN)/{SERIES}/Chapter 1.cbz", CBZ)  # the root is `mangas` itself
        assert states(tree_of(env).import_tree()) == [("stored", None)]


@pytest.mark.req("EF-INT-009")
def test_a_series_without_a_binding_is_left_alone_and_makes_no_work() -> None:
    """Nothing is guessed: no Work is made from a folder name (a purged Work stays purged)."""
    with import_env() as env:
        env.drop("mangas/Source (EN)/Another series/Chapter 1.cbz", CBZ)
        works = sa.select(sa.func.count()).select_from(catalog.work)
        with env.lib.engine.connect() as connection:
            count = connection.execute(works).scalar_one()
        report = tree_of(env).import_tree()
        assert states(report) == [("skipped", "import.tree_no_series")]
        with env.lib.engine.connect() as connection:
            assert connection.execute(works).scalar_one() == count
        assert env.contents() == []


@pytest.mark.req("EF-INT-009")
def test_two_bindings_with_the_same_title_are_not_guessed() -> None:
    with import_env() as env:
        env.drop(f"{CH}/Chapter 5.cbz", CBZ)
        with env.lib.engine.begin() as connection:
            item = acquisition.source_item
            second = connection.execute(
                sa.select(item.c.id).where(item.c.remote_id == "series-1")
            ).scalar_one()
            row = dict(
                connection.execute(sa.select(item).where(item.c.id == second)).mappings().one()
            )
            row.update(id="01J00000000000000000000001", remote_id="series-2")
            connection.execute(sa.insert(item).values(**row))
            binding = acquisition.source_binding
            line = dict(
                connection.execute(
                    sa.select(binding).where(binding.c.id == env.lib.chain["binding"])
                )
                .mappings()
                .one()
            )
            line.update(id="01J00000000000000000000002", source_item_id=row["id"], priority=2)
            connection.execute(sa.insert(binding).values(**line))
        report = tree_of(env).import_tree()
        assert states(report) == [("skipped", "import.tree_ambiguous_series")]


@pytest.mark.req("EF-INT-009")
def test_the_same_title_under_another_source_is_not_the_series_of_the_binding() -> None:
    """BLOCKING of the review: the `<source>` folder is part of the identity. A child's Work
    bound to source 1 never takes the chapters of an adult source with the same title."""
    with import_env() as env:
        env.drop(f"mangas/Adult Source (EN)/{SERIES}/Chapter 7.cbz", CBZ)
        page_folder_in(env, "mangas/Adult Source (EN)", "Chapter 8", {"1.png": PAGE_A})
        with env.lib.engine.begin() as c:
            c.execute(sa.update(catalog.work).values(age_rating=6))
        report = tree_of(env).import_tree()
        assert states(report) == [("skipped", "import.tree_wrong_source")] * 2
        assert env.contents() == [] and env.entries() == {}


def page_folder_in(env: ImportEnv, source: str, chapter: str, pages: dict[str, bytes]) -> None:
    for name, data in pages.items():
        env.drop(f"{source}/{SERIES}/{chapter}/{name}", data)


@pytest.mark.req("EF-INT-009", "EF-BIB-055")
def test_a_source_that_suggests_an_age_above_the_work_holds_the_chapter_to_rate() -> None:
    with import_env() as env:
        env.drop(f"{CH}/Chapter 7.cbz", CBZ)
        page_folder(env, "Chapter 8", {"1.png": PAGE_A})
        with env.lib.engine.begin() as c:
            c.execute(sa.update(catalog.work).values(age_rating=6))
        tree = tree_of(env)
        seed_source(env.lib.engine, warning="nsfw")
        report = tree.import_tree()
        assert states(report) == [("refused", "import.tree_needs_rating")] * 2
        assert env.contents() == [] and env.entries() == {}  # waits: nothing is "decided"
        with env.lib.engine.begin() as c:  # an admin rates the Work 18+ (X): it is let in
            c.execute(sa.update(catalog.work).values(age_rating=18, age_x=True))
        assert states(tree.import_tree()) == [("stored", None)] * 2


HELD_TAGS = [
    "<AgeRating>Adults Only 18+</AgeRating>",
    "<AgeRating>X18+</AgeRating>",
    "<AgeRating>R18+</AgeRating>",
    "<AgeRating>Mature 17+</AgeRating>",
    "<AgeRating>Adult</AgeRating>",
    "<AgeRating>M18</AgeRating>",
    "<AgeRating>18+</AgeRating>",
    "<AgeRating>Unknown</AgeRating>",
    "<AgeRating>Rating Pending</AgeRating>",
    "<AgeRating></AgeRating>",
    "<AgeRating/>",
    "<AgeRating>\u00a0</AgeRating>",
    "<AgeRating>Adults\u00a0Only 18+</AgeRating>",
    "<agerating>X18+</agerating>",
    "<AGERATING>Adults Only 18+</AGERATING>",
    "<AgeRating>Garbage</AgeRating>",
    '<c:AgeRating xmlns:c="urn:x">Adult</c:AgeRating>',
    "<AgeRating>Everyone</AgeRating><AgeRating>Adults Only 18+</AgeRating>",
    "<AgeRating>Everyone</AgeRating><AgeRating>whatever</AgeRating>",
    "<AgeRating>Adults Only 18+</AgeRating><AgeRating>Everyone</AgeRating>",
    "<AgeRating>Everyone</AgeRating><agerating>M18</agerating>",
]


@pytest.mark.req("EF-INT-009", "EF-BIB-055")
@pytest.mark.parametrize("tags", HELD_TAGS)
def test_a_comicinfo_age_above_or_unrecognised_holds_the_chapter_to_rate(tags: str) -> None:
    info = f"<ComicInfo>{tags}</ComicInfo>".encode()
    with import_env() as env:
        env.drop(f"{CH}/Chapter 7.cbz", cbz_bytes(comicinfo=info))
        with env.lib.engine.begin() as c:
            c.execute(sa.update(catalog.work).values(age_rating=6))
        report = tree_of(env).import_tree()
        assert states(report) == [("refused", "import.tree_needs_rating")], tags
        assert env.contents() == [] and env.entries() == {}


@pytest.mark.req("EF-INT-009", "EF-BIB-055")
@pytest.mark.parametrize(
    "warning", [None, "", "NSFW", "nsfw", "Mixed", "mixed", "unknown", "adult", "nsfw\u00a0", "n/a"]
)
def test_a_source_warning_that_is_not_safe_holds_the_chapter_to_rate(warning: str | None) -> None:
    """NULL, another case, a value outside `safe`/`mixed`/`nsfw`: none is known to be safe."""
    with import_env() as env:
        env.drop(f"{CH}/Chapter 7.cbz", CBZ)
        page_folder(env, "Chapter 8", {"1.png": PAGE_A})
        with env.lib.engine.begin() as c:
            c.execute(sa.update(catalog.work).values(age_rating=6))
        tree = tree_of(env)
        seed_source(env.lib.engine, warning=warning)
        assert states(tree.import_tree()) == [("refused", "import.tree_needs_rating")] * 2
        assert env.contents() == [] and env.entries() == {}


@pytest.mark.req("EF-INT-009", "EF-BIB-055")
@pytest.mark.parametrize("warning", ["safe", "SAFE", " Safe "])
def test_a_safe_source_without_an_age_is_stored_as_the_follow_does(warning: str) -> None:
    """The follow cycle files a chapter of a rated series from a `safe` source with no age
    check (`content_warning_age("safe")` is no suggestion): the tree does the same."""
    with import_env() as env:
        env.drop(f"{CH}/Chapter 7.cbz", CBZ)
        with env.lib.engine.begin() as c:
            c.execute(sa.update(catalog.work).values(age_rating=6))
        tree = tree_of(env)
        seed_source(env.lib.engine, warning=warning)
        assert states(tree.import_tree()) == [("stored", None)]


@pytest.mark.req("EF-INT-009")
@pytest.mark.parametrize("folder", ["S.ource (EN)", "Sou rce (EN)", "Source-(EN)", "Sourcee (EN)"])
def test_a_source_folder_is_compared_without_dropping_characters(folder: str) -> None:
    with import_env() as env:
        env.drop(f"mangas/{folder}/{SERIES}/Chapter 7.cbz", CBZ)
        assert states(tree_of(env).import_tree()) == [("skipped", "import.tree_wrong_source")]


@pytest.mark.req("EF-INT-009")
@pytest.mark.parametrize("folder", ["source (en)", "SOURCE (EN)", "Source  (EN)", "Source"])
def test_a_source_folder_in_another_case_or_spacing_still_matches(folder: str) -> None:
    with import_env() as env:
        env.drop(f"mangas/{folder}/{SERIES}/Chapter 7.cbz", CBZ)
        assert states(tree_of(env).import_tree()) == [("stored", None)]


@pytest.mark.req("EF-INT-009", "EF-BIB-055")
def test_a_comicinfo_age_within_the_work_rating_is_stored() -> None:
    info = b"<ComicInfo><AgeRating>Everyone</AgeRating></ComicInfo>"
    with import_env() as env:
        env.drop(f"{CH}/Chapter 7.cbz", cbz_bytes(comicinfo=info))
        with env.lib.engine.begin() as c:
            c.execute(sa.update(catalog.work).values(age_rating=6))
        assert states(tree_of(env).import_tree()) == [("stored", None)]


@pytest.mark.req("EF-INT-009")
def test_a_number_is_never_guessed_out_of_a_volume_an_extra_or_a_year() -> None:
    for name in ["Vol.2 Extra", "Volume 3", "Oneshot 2023", "Bonus 1", "Extras", "2023"]:
        assert chapter_number(name) is None, name
    assert chapter_number("Team_Ch.12 - Part 2") == "12"
    assert chapter_number("Chapter 12.5.cbz") == "12.5"


@pytest.mark.req("EF-INT-009")
def test_two_imports_of_a_tree_never_cross(monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", CBZ)
        fd = os.open(env.lib.tmp / "staging", os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            report = tree_of(env).import_tree()
            assert report.outcomes == () and env.contents() == []
        finally:
            os.close(fd)
        assert states(tree_of(env).import_tree()) == [("stored", None)]


@pytest.mark.req("EF-INT-009")
def test_a_chapter_without_a_readable_number_is_skipped() -> None:
    with import_env() as env:
        env.drop(f"{CH}/Extras.cbz", CBZ)
        assert states(tree_of(env).import_tree()) == [("skipped", "import.tree_unnumbered")]


@pytest.mark.req("EF-INT-009")
def test_a_chapter_still_being_written_waits() -> None:
    with import_env() as env:
        env.drop(f"{CH}/Chapter 3.cbz", CBZ, age=0)
        page_folder(env, "Chapter 4", {"001.png": PAGE_A}, age=0)
        report = tree_of(env).import_tree()
        assert (
            report.outcomes == () and [v.code for v in report.waiting] == ["import.not_ready"] * 2
        )
        assert env.contents() == []


# -- the tombstones --------------------------------------------------------------------


@pytest.mark.req("EF-INT-009")
def test_tree_import_respects_tombstones() -> None:
    """A purged series, a purged content: refused, never stored, and the tree stays."""
    with import_env() as env:
        env.drop(f"{CH}/Chapter 7.cbz", CBZ)
        before = snapshot(env.incoming)
        report = tree_of(env, Gate(series=True)).import_tree()
        assert states(report) == [("refused", "import.tombstoned")]
        assert env.contents() == [] and snapshot(env.incoming) == before
    with import_env() as env:
        env.drop(f"{CH}/Chapter 7.cbz", CBZ)
        page_folder(env, "Chapter 8", {"001.png": PAGE_A})
        report = tree_of(env, Gate(content=True)).import_tree()
        assert states(report) == [("refused", "import.tombstoned")] * 2
        assert env.contents() == []


@pytest.mark.req("EF-INT-009")
def test_a_purged_content_in_the_store_is_refused_too() -> None:
    """Even where the precheck is bypassed, the store refuses a tombstoned content."""
    with import_env() as env:
        env.router.stores[".cbz"]._tombstones = Gate(content=True)
        env.drop(f"{CH}/Chapter 7.cbz", CBZ)
        report = tree_of(env, precheck=None).import_tree()
        assert states(report) == [("refused", "import.tombstoned")]
        assert env.contents() == []


# -- the image folders -----------------------------------------------------------------


@pytest.mark.req("EF-INT-009")
def test_image_folder_packaged_as_cbz_through_allowlist() -> None:
    """The folder is packaged by `write_cbz`: pages renamed in reading order after what their
    bytes are, one ComicInfo at the end, and the result is read back through the allowlist."""
    with import_env() as env:
        page_folder(env, "Chapter 2", {"10.jpg": PAGE_B, "9.png": PAGE_A, ".DS_Store": b"x"})
        report = tree_of(env).import_tree()
        assert states(report) == [("stored", None)]
        (stored,) = [p for p in env.lib.files_under(env.lib.library) if p.suffix == ".cbz"]
        with zipfile.ZipFile(stored) as archive:
            assert archive.namelist() == ["00001.png", "00002.jpg", "ComicInfo.xml"]
            assert archive.read("00001.png") == PAGE_A  # natural order: 9 before 10
            assert b"<Number>2</Number>" in archive.read("ComicInfo.xml")


@pytest.mark.req("EF-INT-009")
@pytest.mark.parametrize(
    "pages",
    [
        {"1.png": PAGE_A, "notes.txt": b"hello"},  # not an image
        {"1.png": b"<script>alert(1)</script>"},  # named png, is a script
        {"1.png": b"MZ\x90\x00" + b"\x00" * 64},  # an executable under an image name
        {"1.png": PAGE_A, "2.svg": b"<svg onload='x()'/>"},
    ],
)
def test_an_image_folder_with_anything_else_is_refused_whole(pages: dict[str, bytes]) -> None:
    with import_env() as env:
        page_folder(env, "Chapter 2", pages)
        before = snapshot(env.incoming)
        report = tree_of(env).import_tree()
        assert states(report) == [("refused", "import.archive_refused")]
        assert env.contents() == [] and snapshot(env.incoming) == before


@pytest.mark.req("EF-INT-009")
def test_a_polyglot_image_goes_through_the_untrusted_chain_and_is_never_executed() -> None:
    """Valid PNG magic with a ZIP and a script behind it is, for the allowlist, an image: it is
    stored as an opaque page (never opened as an archive, never executed), under a name of ours."""
    polyglot = PNG + b"\x00" * 8 + b"PK\x03\x04" + b"<?php system($_GET[0]); ?>"
    with import_env() as env:
        page_folder(env, "Chapter 2", {"evil.php.png": polyglot})
        report = tree_of(env).import_tree()
        assert states(report) == [("stored", None)]
        (stored,) = [p for p in env.lib.files_under(env.lib.library) if p.suffix == ".cbz"]
        with zipfile.ZipFile(stored) as archive:
            assert archive.namelist() == ["00001.png", "ComicInfo.xml"]  # no name of the sender


@pytest.mark.req("EF-INT-009")
def test_a_giant_image_is_refused_and_never_read_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tree_import, "MAX_PAGE_BYTES", 1024)
    with import_env() as env:
        page_folder(env, "Chapter 2", {"1.png": PNG + b"\x00" * 4096})
        report = tree_of(env).import_tree()
        assert states(report) == [] and [v.code for v in report.refused] == ["import.too_large"]
        assert env.contents() == []


@pytest.mark.req("EF-INT-009")
def test_a_chapter_above_the_size_limit_is_refused() -> None:
    with import_env() as env:
        page_folder(env, "Chapter 2", {"1.png": PAGE_A, "2.png": PAGE_A})
        report = tree_of(env, max_bytes=len(PAGE_A) + 1).import_tree()
        assert states(report) == [("refused", "import.too_large")]
        assert env.contents() == []


@pytest.mark.req("EF-INT-009")
@pytest.mark.parametrize(
    "name", ["..\\..\\evil.png", "a\x1bb.png", "‮gnp.exe", "x.png ", "..", "a\\b.png"]
)
def test_an_image_name_that_climbs_is_refused_and_never_reaches_a_path(name: str) -> None:
    with import_env() as env:
        folder = page_folder(env, "Chapter 2", {"1.png": PAGE_A})
        if name != "..":
            (folder / name).write_bytes(PAGE_B)
            os.utime(folder / name, (1, 1))
        else:
            (folder / "..").exists()  # `..` cannot be created: the entry below is its stand-in
            (folder / "...").write_bytes(PAGE_B)
            os.utime(folder / "...", (1, 1))
        report = tree_of(env).import_tree()
        codes = refusal_codes(report)
        assert codes and all(c == "import.unsafe_name" for c in codes), codes
        assert env.contents() == []
        assert not list(env.lib.library.rglob("evil*"))


@pytest.mark.req("EF-INT-009")
def test_a_traversing_folder_name_refuses_what_is_under_it() -> None:
    with import_env() as env:
        env.drop(f"mangas/Source (EN)/{SERIES}/..\\..\\x/1.png", PAGE_A)
        report = tree_of(env).import_tree()
        assert report.outcomes == () and [v.code for v in report.refused] == ["import.unsafe_name"]
        assert env.contents() == []


# -- links and special files -----------------------------------------------------------


@pytest.mark.req("EF-INT-009")
def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.cbz").write_bytes(CBZ)
    (outside / "pic.png").write_bytes(PAGE_A)
    with import_env() as env:
        series = env.incoming / CH
        series.mkdir(parents=True)
        os.symlink(outside / "secret.cbz", series / "Chapter 1.cbz")  # a file link
        os.symlink(outside, series / "Chapter 2")  # a folder link
        folder = series / "Chapter 3"
        folder.mkdir()
        env.drop(f"{CH}/Chapter 3/1.png", PAGE_A)
        os.symlink(outside / "pic.png", folder / "2.png")  # a link among the pages
        os.symlink(outside, env.incoming / "mangas" / "Linked source")  # a linked source
        report = tree_of(env).import_tree()
        codes = sorted(refusal_codes(report))
        assert codes == ["import.link_refused"] * 4, codes
        assert env.contents() == [] and (outside / "secret.cbz").read_bytes() == CBZ


@pytest.mark.req("EF-INT-009")
def test_special_files_and_hard_links_are_refused() -> None:
    with import_env() as env:
        tmp_path = env.lib.tmp  # the same disk: a hard link is possible
        (tmp_path / "elsewhere.cbz").write_bytes(CBZ)
        series = env.incoming / CH
        series.mkdir(parents=True)
        os.mkfifo(series / "Chapter 1.cbz")  # a pipe where a file should be
        os.link(tmp_path / "elsewhere.cbz", series / "Chapter 2.cbz")  # a hard link
        folder = series / "Chapter 3"
        folder.mkdir()
        env.drop(f"{CH}/Chapter 3/1.png", PAGE_A)
        os.mkfifo(folder / "2.png")
        report = tree_of(env).import_tree()
        assert sorted(refusal_codes(report)) == [
            "import.link_refused", "import.unsupported_file", "import.unsupported_file"
        ]  # fmt: skip
        assert env.contents() == [] and (tmp_path / "elsewhere.cbz").read_bytes() == CBZ


@pytest.mark.req("EF-INT-009")
def test_a_tree_that_is_or_holds_a_root_of_ours_is_refused() -> None:
    with import_env() as env, pytest.raises(Exception, match=r"overlaps|is a root"):
        SuwayomiTree(
            env.lib.library, targets=TreeTargets(env.lib.engine, SystemClock(), NONE_PURGED),
            engine=env.lib.engine, fsops=env.lib.fsops, store=env.router,
            staging_dir=env.lib.tmp / "staging", place=lambda *_: "",
            keep_apart=(env.lib.library,),
        )  # fmt: skip


# -- a protected file, a file that is not a chapter ------------------------------------


@pytest.mark.req("EF-INT-009")
def test_a_protected_file_named_as_a_chapter_is_refused_and_stays() -> None:
    from shelfhold.sdk.testing.corpus.trapped_epubs.build import Member
    from tests.import_env import AES, encryption_xml, epub_with

    protected = epub_with(Member("META-INF/encryption.xml", encryption_xml(AES, "OEBPS/c.xhtml")))
    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", protected)
        report = tree_of(env).import_tree()
        assert states(report) == [("refused", "import.protected")]
        assert (env.incoming / CH / "Chapter 1.cbz").read_bytes() == protected
        assert env.contents() == []


@pytest.mark.req("EF-INT-009")
def test_a_plain_epub_named_as_a_chapter_is_not_filed_as_a_comic() -> None:
    from tests.import_env import CLEAN_EPUB

    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", CLEAN_EPUB)
        report = tree_of(env).import_tree()
        assert states(report) == [("refused", "import.unsupported_file")]
        assert env.contents() == []


@pytest.mark.req("EF-INT-009")
def test_a_cbz_off_the_allowlist_is_refused() -> None:
    bad = cbz_bytes(pages=(("1.png", PNG), ("run.sh", b"#!/bin/sh\n")))
    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", bad)
        assert states(tree_of(env).import_tree()) == [("refused", "import.archive_refused")]
        assert env.contents() == []


# -- duplicates ------------------------------------------------------------------------


@pytest.mark.req("EF-INT-009")
def test_a_chapter_already_stored_is_a_duplicate_not_a_second_variant() -> None:
    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", CBZ)
        assert states(tree_of(env).import_tree()) == [("stored", None)]
        # the same chapter again, and the same number under other bytes
        other = cbz_bytes(pages=(("1.png", PNG + b"z"),))
        env.drop(f"{CH}/Ch.1 - bis.cbz", other)
        report = tree_of(env).import_tree()
        assert sorted(states(report)) == [("skipped", "import.tree_duplicate")] * 2
        assert len(env.contents()) == 1
        with env.lib.engine.connect() as connection:
            variants = connection.execute(
                sa.select(sa.func.count()).select_from(catalog.unit_variant)
            )
            assert variants.scalar_one() == 1


@pytest.mark.req("EF-INT-009")
def test_importing_twice_changes_nothing() -> None:
    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", CBZ)
        page_folder(env, "Chapter 2", {"1.png": PAGE_A})
        tree_of(env).import_tree()
        first = [(c.id, c.original_sha256) for c in env.contents()]
        tree_of(env).import_tree()
        assert [(c.id, c.original_sha256) for c in env.contents()] == first


@pytest.mark.req("EF-INT-009")
def test_a_refusal_is_kept_apart_from_the_import_folders_own() -> None:
    """The decisions of a tree are filed under their own prefix: they never answer for a file of
    the import folder that has the same relative name."""
    with import_env() as env:
        env.drop(f"{CH}/Chapter 1.cbz", b"not an archive at all")
        tree_of(env).import_tree()
        assert env.entries() and all(
            key.startswith(tree_import.ENTRY_PREFIX) for key in env.entries()
        )
