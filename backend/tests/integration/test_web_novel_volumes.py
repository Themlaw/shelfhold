"""M2 L26 (EF-BIB-040, EF-SRC-032, EF-LEC-014, EF-BIB-044): web-novel volumes, kept and exported.

The files are the production ones (`<data>/ranged/` written by the keeper, neutralized on the way
out); the provider is a fake that leaves one `export_item` per volume. Each test attacks one rule:
a volume exported before it is closed, a tail rewritten under a reader or without the upgrade, a
table of contents that makes an incomplete file look complete, a path that leaves the root.
"""

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import Bound
from shelfhold.export.application.ranged_export import (
    PreparedRange,
    RangedExporter,
    RangedExportError,
    ServedCopy,
    locate_ranged,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, reading
from shelfhold.infrastructure.volumes import write_data_file
from tests.library_env import LibEnv, lib_env
from tests.ranged_env import RangedBench, ranged_bench, signer, volume_epub

ARGON = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"


@pytest.fixture
def lib() -> Iterator[LibEnv]:
    with lib_env() as env:
        yield env


class Doors:
    """The two ports of the exporter, recording what went through them."""

    def __init__(self) -> None:
        self.placed: list[str] = []
        self.replaced: list[str] = []

    def place(self, prepared: PreparedRange) -> None:
        self.placed.append(prepared.source_hash)

    def replace(self, prepared: PreparedRange) -> None:
        self.replaced.append(prepared.source_hash)


def exporter(lib: LibEnv, **kw: float) -> RangedExporter:
    return RangedExporter(lib.engine, lib.data, **kw)


def run(
    lib: LibEnv, bench: RangedBench, doors: Doors, served: ServedCopy | None = None
) -> tuple[str, str | None, str | None]:
    out = exporter(lib).export(
        bench.item_id, served=served, place=doors.place, replace=doors.replace
    )
    return out.action, out.reason, out.warning


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_by_a_reader(lib: LibEnv, item_id: str) -> None:
    """A reader reached the end of the file (the position of the file, `percent = 1`)."""
    user = new_ulid()
    with lib.engine.begin() as connection:
        connection.execute(
            sa.insert(identity.user).values(
                id=user, username="reader", role="admin", can_rate=True, password_hash=ARGON
            )
        )
        connection.execute(
            sa.insert(reading.reading_position).values(
                id=new_ulid(), user_id=user, carrier_type="export_item", carrier_id=item_id,
                content_hash="sha256:" + "a" * 64, locator={"progression": 1.0}, percent=1.0,
                client_ts=sa.func.current_timestamp(),
            )
        )  # fmt: skip


# -- EF-BIB-040: the policy ------------------------------------------------------------------


@pytest.mark.req("EF-BIB-040", "EF-SRC-032")
def test_closed_only_open_volume_never_exported(lib: LibEnv) -> None:
    bench = ranged_bench(lib, closed=False)  # the default policy, a volume still open
    doors = Doors()
    assert run(lib, bench, doors) == ("waited", "volume_open", None)
    assert doors.placed == [] and doors.replaced == []
    ranged_bench(lib, closed=False, status="finished")  # a finished novel changes nothing
    assert run(lib, bench, doors)[0] == "waited" and doors.placed == []


@pytest.mark.req("EF-BIB-040", "EF-SRC-032")
def test_closed_volume_hash_never_changes_on_new_chapters(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 3, closed=True)
    doors = Doors()
    assert run(lib, bench, doors)[0] == "placed"
    (served_hash,) = doors.placed
    served = ServedCopy(served_hash)
    kept_before = sha(bench.kept())
    # chapters 4 and 5 arrive: they go to the next volume, the closed one is not touched
    tail = ranged_bench(lib, 2, closed=False, policy="rolling_tail")
    assert run(lib, tail, doors)[0] == "placed"
    assert run(lib, bench, doors, served) == ("kept", "volume_closed", None)
    # the source reproduces the closed volume with other bytes: still frozen, not even read
    write_data_file(lib.data, f"ranged/{bench.item_id}.epub", volume_epub(3, extra_items=(2,)))
    assert sha(bench.kept()) != kept_before
    assert run(lib, bench, doors, served) == ("kept", "volume_closed", None)
    assert len(doors.placed) == 2 and doors.replaced == []  # the closed one never went again


@pytest.mark.req("EF-BIB-040", "EF-SRC-032")
def test_rolling_tail_replaced_same_path_with_warning(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 2, closed=False, policy="rolling_tail")
    doors = Doors()
    action, _, warning = run(lib, bench, doors)
    assert action == "placed" and warning == "volume_tail_replaced"
    (first,) = doors.placed
    # a chapter joins the volume: the same item, the file kept grows, the replacement goes
    # through the upgrade door (never through a plain placement over the served copy)
    with lib.engine.begin() as connection:
        from tests.integration.test_schema_catalog import add_unit

        last = connection.execute(
            sa.text(
                "SELECT max(number_sort) FROM unit u JOIN unit_coverage c ON c.unit_id = u.id "
                "WHERE c.carrier_id = :i"
            ),
            {"i": bench.item_id},
        ).scalar_one()
        unit = add_unit(connection, bench.edition, str(int(last) + 1))
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=bench.item_id, unit_id=unit, position=2,
                extent="full", origin="provider", start_locator="OEBPS/ch3.xhtml",
                end_locator="OEBPS/ch3.xhtml",
            )
        )  # fmt: skip
    write_data_file(lib.data, f"ranged/{bench.item_id}.epub", volume_epub(3))
    action, _, warning = run(lib, bench, doors, ServedCopy(first, spine_count=2))
    assert (action, warning) == ("replaced", "volume_tail_replaced")
    assert len(doors.placed) == 1 and len(doors.replaced) == 1
    assert doors.replaced[0] != first
    # nothing new: nothing to replace
    assert run(lib, bench, doors, ServedCopy(doors.replaced[0]))[:2] == ("kept", "up_to_date")


@pytest.mark.req("EF-BIB-040", "EF-EXP-016")
def test_rolling_tail_never_rewrites_a_read_volume(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 2, closed=False, policy="rolling_tail")
    doors = Doors()
    run(lib, bench, doors)
    served = ServedCopy("sha256:" + "c" * 64, skip_read=True)
    read_by_a_reader(lib, bench.item_id)
    assert run(lib, bench, doors, served) == ("kept", "keep_read", None)
    assert doors.replaced == []
    # the shelf that opts in to upgrading read files gets it, still through the upgrade door
    assert run(lib, bench, doors, ServedCopy(served.file_hash, skip_read=False))[0] == "replaced"


@pytest.mark.req("EF-BIB-040", "EF-BIB-024")
def test_rolling_tail_suspect_replacement_goes_through_the_guard(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 2, closed=False, policy="rolling_tail")
    doors = Doors()
    served = ServedCopy("sha256:" + "d" * 64, spine_count=40)  # the copy served is much longer
    with pytest.raises(RangedExportError) as refused:
        run(lib, bench, doors, served)
    assert refused.value.code == "ranged.suspect_revision"
    assert doors.replaced == [] and doors.placed == []


@pytest.mark.req("EF-BIB-040")
@pytest.mark.parametrize("policy", ["closed_only", "whole"])
def test_policy_without_tail_keeps_an_open_volume_already_served(lib: LibEnv, policy: str) -> None:
    bench = ranged_bench(lib, 2, closed=False, policy=policy, status="finished")
    doors = Doors()
    action, reason, _ = run(lib, bench, doors, ServedCopy("sha256:" + "e" * 64))
    assert (action, reason) == ("kept", "policy_keeps_served") and doors.replaced == []


@pytest.mark.req("EF-BIB-040")
def test_whole_policy_only_for_finished_novels(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 3, closed=True, policy="whole", status="ongoing")
    doors = Doors()
    with pytest.raises(RangedExportError) as refused:
        run(lib, bench, doors)
    assert refused.value.code == "ranged.policy_refuses" and doors.placed == []
    for status in ("paused", "unknown"):  # only "finished" counts
        ranged_bench(lib, 3, policy="whole", status=status)
    bench = ranged_bench(lib, 3, closed=True, policy="whole", status="finished")
    assert run(lib, bench, doors)[0] == "placed"


# -- EF-LEC-014: bounds from the table of contents -------------------------------------------


@pytest.mark.req("EF-LEC-014")
def test_epub_range_coverage_bounds_from_toc(lib: LibEnv) -> None:
    from shelfhold.catalog.public import CoverageMismatchError, bounds_from_toc, verify_item
    from shelfhold.infrastructure.packaging.epub import read_toc

    for ncx in (False, True):  # an EPUB 3 nav and an EPUB 2 ncx read alike
        data = volume_epub(4, ncx=ncx, extra_items=(2,))
        path = lib.tmp / f"v{ncx}.epub"
        path.write_bytes(data)
        toc = read_toc(path)
        assert [c.title for c in toc.chapters] == [f"Chapter {n}" for n in range(1, 5)]
        assert toc.spine[2] == "OEBPS/ch2b.xhtml" and not toc.unresolved
        units = [new_ulid() for _ in range(4)]
        bounds = bounds_from_toc(toc, units)
        # chapter 2 runs over its second spine item: the bound is the item before chapter 3
        assert (bounds[1].start, bounds[1].end) == ("OEBPS/ch2.xhtml", "OEBPS/ch2b.xhtml")
        assert (bounds[0].start, bounds[0].end) == ("OEBPS/ch1.xhtml", "OEBPS/ch1.xhtml")
        with pytest.raises(CoverageMismatchError, match="4 chapters"):
            bounds_from_toc(toc, units[:3])  # nothing is guessed when the counts differ
    # the source gave no bounds: they are read from the table when the counts agree
    bench = ranged_bench(lib, 3, declare_bounds=False)
    with lib.engine.connect() as connection:
        path = lib.tmp / "v3.epub"
        path.write_bytes(volume_epub(3))
        assert verify_item(connection, bench.item_id, read_toc(path)).complete
    with RangedExporter(lib.engine, lib.data).prepare(bench.item_id) as prepared:
        assert prepared.report.complete and prepared.closed and prepared.spine_count == 3


def _toc_of(lib: LibEnv, data: bytes):  # type: ignore[no-untyped-def]
    from shelfhold.infrastructure.packaging.epub import read_toc

    path = lib.tmp / f"{new_ulid()}.epub"
    path.write_bytes(data)
    return read_toc(path)


@pytest.mark.req("EF-LEC-014")
def test_toc_hole_overlap_and_duplicate_never_present_as_complete(lib: LibEnv) -> None:
    from shelfhold.catalog.public import Bound, check_coverage

    toc = _toc_of(lib, volume_epub(4))
    a, b, c, d = (
        Bound(new_ulid(), f"OEBPS/ch{n}.xhtml", f"OEBPS/ch{n}.xhtml") for n in range(1, 5)
    )
    assert check_coverage(toc, [a, b, c, d]).complete
    hole = check_coverage(toc, [a, b, d])  # chapter 3 is carried by nobody
    assert not hole.complete and hole.gaps == ("Chapter 3",)
    wide = Bound(b.unit_id, b.start, "OEBPS/ch3.xhtml")
    overlap = check_coverage(toc, [a, wide, c, d])  # two Units share chapter 3
    assert not overlap.complete and overlap.overlaps == ((b.unit_id, c.unit_id),)
    twice = check_coverage(toc, [a, b, b, c, d])  # one Unit declared twice
    assert not twice.complete and twice.duplicates == (b.unit_id,)
    listed_twice = _toc_of(
        lib,
        volume_epub(
            3,
            nav=[
                ("One", "ch1.xhtml"),
                ("Two", "ch2.xhtml"),
                ("Two", "ch2.xhtml"),
                ("Three", "ch3.xhtml"),
            ],
        ),
    )
    report = check_coverage(
        listed_twice, [Bound(new_ulid(), f"OEBPS/ch{n}.xhtml", None) for n in (1, 2, 3)]
    )
    assert not report.complete and report.duplicates == ("Two",)
    lost = check_coverage(toc, [Bound(a.unit_id, "OEBPS/nowhere.xhtml", None), b, c, d])
    assert not lost.complete and lost.unresolved == (a.unit_id,)  # a bound that points at nothing
    back = check_coverage(toc, [Bound(a.unit_id, "OEBPS/ch2.xhtml", "OEBPS/ch1.xhtml"), b, c, d])
    assert not back.complete and back.unresolved == (a.unit_id,)  # bounds back to front
    assert not check_coverage(toc, [Bound(a.unit_id, None, None), b, c, d]).complete  # unverified
    assert not check_coverage(
        toc, [a, b, c, d, Bound(new_ulid(), "OEBPS/ch4.xhtml", None)]
    ).complete
    assert not check_coverage(toc, []).complete  # nothing declared is not complete
    empty = _toc_of(lib, volume_epub(0))
    assert not check_coverage(empty, []).complete  # a file with no chapter is never complete


@pytest.mark.req("EF-LEC-014", "EF-SRC-032")
@pytest.mark.parametrize(
    "case", ["hole", "chapter_not_declared", "no_bounds_count_differs", "toc_lists_extra"]
)
def test_incomplete_epub_is_refused_not_exported(lib: LibEnv, case: str) -> None:
    doors = Doors()
    if case == "hole":  # the file has three chapters, the item declares chapters 1 and 3
        bench = ranged_bench(lib, 3, declared=[1, 3])
    elif case == "chapter_not_declared":  # four chapters in the file, the item declares three
        bench = ranged_bench(lib, 3, data=volume_epub(4))
    elif case == "no_bounds_count_differs":  # no bounds from the source, counts disagree
        bench = ranged_bench(lib, 3, declare_bounds=False, data=volume_epub(5))
    else:  # the table lists a chapter whose file is not in the reading order
        bench = ranged_bench(
            lib,
            3,
            data=volume_epub(
                3, nav=[(f"C{n}", f"ch{n}.xhtml") for n in (1, 2, 3)] + [("Ghost", "ghost.xhtml")]
            ),
        )
    with pytest.raises(RangedExportError) as refused:
        run(lib, bench, doors)
    assert refused.value.code == "ranged.coverage_incomplete"
    assert doors.placed == [] and doors.replaced == []


@pytest.mark.req("EF-LEC-014", "ENF-SEC-014")
def test_toc_with_hostile_links_and_entities_is_not_followed(lib: LibEnv) -> None:
    from shelfhold.infrastructure.packaging.epub import TocError

    toc = _toc_of(
        lib,
        volume_epub(
            2, nav=[("One", "ch1.xhtml"), ("Out", "../../etc/passwd"), ("Abs", "/etc/passwd"),
                    ("Net", "http://example.invalid/x"), ("Two", "ch2.xhtml")],
        ),
    )  # fmt: skip
    assert [c.title for c in toc.chapters] == ["One", "Two"]
    assert len(toc.unresolved) == 3  # listed, never opened
    from shelfhold.catalog.public import Bound, check_coverage

    both = [Bound(new_ulid(), f"OEBPS/ch{n}.xhtml", f"OEBPS/ch{n}.xhtml") for n in (1, 2)]
    assert len(check_coverage(toc, both).gaps) == 3  # and not a complete file either
    bomb = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
    assert _toc_of(lib, volume_epub(1)).chapters  # the well-formed baseline reads
    hostile = volume_epub(1)
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(hostile)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename.endswith("nav.xhtml"):
                body = body.replace(b"<html", bomb.encode() + b"<html", 1).replace(
                    b'<?xml version="1.0"?>', b""
                )
            dst.writestr(info, body)
    with pytest.raises(TocError):
        _toc_of(lib, out.getvalue())


# -- /data/ranged: where the file is ---------------------------------------------------------


@pytest.mark.req("EF-SRC-032", "ENF-SEC-002")
def test_ranged_export_never_leaks_a_path_outside_the_root(lib: LibEnv, tmp_path: Path) -> None:
    secret = tmp_path / "outside.epub"
    secret.write_bytes(volume_epub(3))
    bench = ranged_bench(lib, 3)
    doors = Doors()
    kept = bench.kept()
    kept.unlink()
    kept.symlink_to(secret)  # the kept file became a link to a file outside the root
    with pytest.raises(RangedExportError) as refused:
        run(lib, bench, doors)
    assert refused.value.code == "ranged.outside_root" and doors.placed == []
    kept.unlink()
    folder = lib.data / "ranged"
    moved = lib.data / "ranged-real"
    folder.rename(moved)
    folder.symlink_to(moved)  # the whole folder is a link
    with pytest.raises(RangedExportError) as unsafe:
        run(lib, bench, doors)
    assert unsafe.value.code == "ranged.folder_unsafe"
    folder.unlink()
    moved.rename(folder)
    for bad in ("../outside", "../../etc/passwd", "/etc/passwd", f"{bench.item_id}/../x", "x" * 26,
                bench.item_id.lower(), ""):  # fmt: skip
        with pytest.raises(RangedExportError) as bad_id:
            locate_ranged(lib.data, bad)
        assert bad_id.value.code == "ranged.bad_id"
    with pytest.raises(RangedExportError) as missing:
        locate_ranged(lib.data, new_ulid())
    assert missing.value.code == "ranged.missing"
    (folder / f"{bench.item_id}.cbz").write_bytes(b"x")  # another format is not exported here
    with pytest.raises(RangedExportError) as other:
        locate_ranged(lib.data, bench.item_id)
    assert other.value.code == "ranged.not_epub"
    with pytest.raises(RangedExportError) as unknown:  # a ULID that is no export item
        stray = new_ulid()
        write_data_file(lib.data, f"ranged/{stray}.epub", volume_epub(1))
        exporter(lib).export(stray, served=None, place=doors.place, replace=doors.replace)
    assert unknown.value.code == "ranged.unknown_item"
    assert not any(isinstance(p, str) and p.startswith(str(lib.data)) for p in doors.placed)


@pytest.mark.req("EF-SRC-032", "ENF-SEC-002")
def test_ranged_special_file_and_garbage_are_refused(lib: LibEnv) -> None:
    bench = ranged_bench(lib, 3)
    fifo = bench.kept()
    fifo.unlink()
    os.mkfifo(fifo)
    doors = Doors()
    with pytest.raises(RangedExportError) as special:
        run(lib, bench, doors)
    assert special.value.code == "ranged.outside_root"
    fifo.unlink()
    write_data_file(lib.data, f"ranged/{bench.item_id}.epub", b"not an epub")
    with pytest.raises(RangedExportError) as garbage:
        run(lib, bench, doors)
    assert garbage.value.code == "ranged.untrusted_entry"
    write_data_file(lib.data, f"ranged/{bench.item_id}.epub", b"")
    with pytest.raises(RangedExportError):
        run(lib, bench, doors)
    assert doors.placed == []


@pytest.mark.req("EF-SRC-032", "EF-BIB-051")
def test_ranged_export_hands_on_the_neutralized_derivative_only(lib: LibEnv) -> None:
    import io
    import zipfile

    raw = io.BytesIO(volume_epub(2))
    out = io.BytesIO()
    with zipfile.ZipFile(raw) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename.endswith("ch1.xhtml"):
                body = body.replace(b"<p>", b'<script>alert(1)</script><p onclick="x()">')
            dst.writestr(info, body)
    bench = ranged_bench(lib, 2, data=out.getvalue())
    seen: list[bytes] = []

    def place(prepared: PreparedRange) -> None:
        seen.append(prepared.path.read_bytes())

    exporter(lib).export(bench.item_id, served=None, place=place, replace=place)
    (derivative,) = seen
    with zipfile.ZipFile(io.BytesIO(derivative)) as archive:
        text = archive.read("OEBPS/ch1.xhtml").decode()
    assert "<script" not in text and "onclick" not in text
    assert b"<script" in zipfile.ZipFile(bench.kept()).read("OEBPS/ch1.xhtml")  # original kept


@pytest.mark.req("EF-SRC-032", "EF-EXP-016")
def test_served_copy_reads_the_manifest_and_the_shelf_option() -> None:
    from tests.export_env import export_env
    from tests.integration.test_schema_storage_export import add_record

    with export_env() as env:
        bench = ranged_bench(env.lib, 2, closed=False, policy="rolling_tail")
        exporter_ = RangedExporter(env.engine, env.lib.data)
        assert exporter_.served_copy(env.destination_id, bench.item_id) is None  # nothing yet
        with env.engine.begin() as connection:
            add_record(
                connection, env.destination_id, bench.item_id, sidecar_sha256="sha256:" + "f" * 64
            )
        served = exporter_.served_copy(env.destination_id, bench.item_id)
        assert served == ServedCopy("sha256:" + "f" * 64, skip_read=True)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf).values(skip_read_upgrades=False))
        again = exporter_.served_copy(env.destination_id, bench.item_id)
        assert again is not None and again.skip_read is False
        with env.engine.begin() as connection:  # a record that is not written is not served
            connection.execute(sa.update(export.export_record).values(state="planned"))
        assert exporter_.served_copy(env.destination_id, bench.item_id) is None


# -- regressions of the review of #153 -------------------------------------------------------


@pytest.mark.req("EF-LEC-014")
def test_toc_out_of_reading_order_never_complete(lib: LibEnv) -> None:
    from shelfhold.catalog.public import bounds_from_toc, check_coverage

    # chapter 3 of the table points back at the cover, the first item of the reading order
    toc = _toc_of(
        lib,
        volume_epub(
            3, cover=True,
            nav=[
                ("Chapter 1", "ch1.xhtml"), ("Chapter 2", "ch2.xhtml"),
                ("Chapter 3", "cover.xhtml"),
            ],
        ),
    )  # fmt: skip
    report = check_coverage(toc, bounds_from_toc(toc, [new_ulid() for _ in range(3)]))
    assert not report.complete and "Chapter 3" in report.disordered
    # Units declared in an order that goes back in the file
    toc = _toc_of(lib, volume_epub(3))
    ids = [new_ulid() for _ in range(3)]
    paths = [f"OEBPS/ch{n}.xhtml" for n in (1, 3, 2)]
    bounds = [Bound(i, p, p) for i, p in zip(ids, paths, strict=True)]
    swapped = check_coverage(toc, bounds)
    assert not swapped.complete and swapped.disordered == (ids[2],)


@pytest.mark.req("EF-LEC-014")
def test_toc_chapter_copied_under_another_name_or_title_never_complete(lib: LibEnv) -> None:
    from shelfhold.catalog.public import bounds_from_toc, check_coverage

    # chapter 3 is a byte copy of chapter 2 under another file name and another title
    toc = _toc_of(
        lib,
        volume_epub(
            3, copy_of={3: 2},
            nav=[("Prologue", "ch1.xhtml"), ("Interlude", "ch2.xhtml"), ("Epilogue", "ch3.xhtml")],
        ),
    )  # fmt: skip
    report = check_coverage(toc, bounds_from_toc(toc, [new_ulid() for _ in range(3)]))
    assert not report.complete and report.copies == ("Epilogue",)
    # the titles of the table must follow the numbers of the Units, whatever the offset
    bench = ranged_bench(
        lib, 3,
        data=volume_epub(3, nav=[("Chapter 1", "ch1.xhtml"), ("Chapter 2", "ch2.xhtml"),
                                 ("Chapter 2", "ch3.xhtml")]),
    )  # fmt: skip
    with pytest.raises(RangedExportError) as refused:
        run(lib, bench, Doors())
    assert refused.value.code == "ranged.coverage_incomplete" and "mislabeled" in str(refused.value)
    fine = ranged_bench(lib, 3)  # numbers 1..3 under units 4xx: a constant offset is no fault
    assert run(lib, fine, Doors())[0] == "placed"


@pytest.mark.req("EF-BIB-024", "EF-BIB-040")
def test_suspect_guard_live_for_rolling_tail_from_the_manifest() -> None:
    from tests.export_env import export_env
    from tests.integration.test_schema_storage_export import add_record

    with export_env() as env:
        bench = ranged_bench(env.lib, 2, closed=False, policy="rolling_tail")
        ex = RangedExporter(env.engine, env.lib.data)
        with env.engine.begin() as connection:
            add_record(
                connection, env.destination_id, bench.item_id, sidecar_sha256="sha256:" + "f" * 64
            )
        with ex.prepare(bench.item_id) as stub_prepared:
            ex.note_placed(env.destination_id, bench.item_id, stub_prepared)
        served = ex.served_copy(env.destination_id, bench.item_id)
        assert served is not None and served.spine_count == 2  # written down at placement
        # the tail served had 40 chapters: the replacement of 2 is a stub, refused
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_record).values(spine_count=40))
        served = ex.served_copy(env.destination_id, bench.item_id)
        doors = Doors()
        with pytest.raises(RangedExportError) as refused:
            ex.export(bench.item_id, served=served, place=doors.place, replace=doors.replace)
        assert refused.value.code == "ranged.suspect_revision" and doors.replaced == []
        # a Unit the source stubbed refuses it as well, whatever the counts say
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_record).values(spine_count=2))
            connection.execute(
                sa.text("UPDATE unit SET availability = 'stubbed' WHERE id = :u"),
                {"u": bench.unit_ids[0]},
            )
        served = ex.served_copy(env.destination_id, bench.item_id)
        with pytest.raises(RangedExportError) as stubbed:
            ex.export(bench.item_id, served=served, place=doors.place, replace=doors.replace)
        assert stubbed.value.code == "ranged.suspect_revision" and doors.replaced == []


@pytest.mark.req("EF-SRC-032", "ENF-SEC-002")
def test_ranged_folder_swapped_for_a_link_after_the_check_reads_nothing_outside(
    lib: LibEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shelfhold.export.application.ranged_export as module

    bench = ranged_bench(lib, 3)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{bench.item_id}.epub").write_bytes(volume_epub(1))  # a valid book, not ours
    kept_before = bench.kept().read_bytes()
    seen: list[bytes] = []
    real = module.neutralize_epub

    def swap_then_neutralize(source, out, **kw):  # type: ignore[no-untyped-def]
        seen.append(source.getvalue())
        return real(source, out, **kw)

    monkeypatch.setattr(module, "neutralize_epub", swap_then_neutralize)
    real_open = module.open_data_folder
    folder = lib.data / "ranged"

    from contextlib import contextmanager

    @contextmanager
    def swapping(data_dir, name):  # type: ignore[no-untyped-def]
        with real_open(data_dir, name) as handle:
            folder.rename(lib.data / "ranged.old")  # the swap, once the folder is open
            folder.symlink_to(outside)
            yield handle

    monkeypatch.setattr(module, "open_data_folder", swapping)
    run(lib, bench, Doors())
    assert seen and kept_before in [seen[0]]  # what was read is the kept file, not the outside one
    assert seen[0] != (outside / f"{bench.item_id}.epub").read_bytes()


@pytest.mark.req("EF-BIB-040", "EF-LEC-014")
def test_read_state_counts_the_children_of_a_re_cut_chapter(lib: LibEnv) -> None:
    from shelfhold.catalog.public import apply_resplit, propose_resplit
    from tests.integration.test_schema_catalog import add_unit as unit_of

    bench = ranged_bench(lib, 2, closed=False, policy="rolling_tail")
    parent = bench.unit_ids[0]
    with lib.engine.begin() as connection:
        number = connection.execute(
            sa.text("SELECT number_norm FROM unit WHERE id = :u"), {"u": parent}
        ).scalar_one()
        child = unit_of(connection, bench.edition, f"{number}.1")
        proposal = propose_resplit(connection, parent, [child], signer(lib))
        apply_resplit(connection, proposal, signer(lib), confirmation=proposal.token)
    # a reader finished the child on the file of its own (another carrier covers it)
    other = new_ulid()
    with lib.engine.begin() as connection:
        connection.execute(
            sa.insert(export.export_item).values(id=other, edition_id=bench.edition, kind="unit")
        )
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item",
                carrier_id=other,
                unit_id=child,
                position=0,
                extent="full",
                origin="provider",
            )
        )
    read_by_a_reader(lib, other)
    doors = Doors()
    served = ServedCopy("sha256:" + "c" * 64, skip_read=True)
    assert run(lib, bench, doors, served) == ("kept", "keep_read", None)
    assert doors.replaced == []
