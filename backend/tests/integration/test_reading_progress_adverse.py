"""Adversarial review of #284 (M4 L14a, merged `reading_progress`): proofs of concept, red on
purpose (`xfail` strict until fixed: the fix makes them pass, then the marker goes).

Each test states what the merge rule or the alias sweep must do (see `docs/decisions/M4.md`,
« #284 : contradicteur »). They were red proofs of concept; the objections are corrected and the
markers are gone. The second pass (« #284 : second passage ») adds red ones again, at the end.
"""

import contextlib
import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.catalog.application.revisions import default_is_read
from shelfhold.export.application import render_hooks
from shelfhold.export.application.render_hooks import heal_alias_hashes
from shelfhold.infrastructure.schema import export
from shelfhold.reading.application.server_state import record_server_state
from shelfhold.reading.domain.document_hash import koreader_partial_md5
from shelfhold.reading.public import record_position, undo_read_mark
from tests.reading_env import NEW_HASH, OLD_HASH, hours, reading_env

pytestmark = pytest.mark.req("EF-LEC-014")


def states(env, user: str, units: list[str]) -> list[str]:  # type: ignore[no-untyped-def]
    got = env.progress(user)
    return [got[u].state if u in got else "-" for u in units]


def test_b1_a_page_sent_as_text_marks_nothing_beyond_it(tmp_path: Path) -> None:
    """KOReader sends `progress` as `tostring(page)` (ADR-0053): page "1" is text. SQLite ranks
    any TEXT above any INTEGER, so `'1' >= 30` is true and the whole volume reads as read, for
    every reader of `reading_progress` (revisions, upgrade, follows) and for good."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 3)
        volume = env.file("ok", units, bounds=[(1, 10), (11, 20), (21, 30)])
        with env.engine.begin() as connection:
            record_position(
                connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
                carrier_id=volume, content_hash=NEW_HASH, locator={"page": "1"}, percent=None,
                client_ts=hours(1),
            )  # fmt: skip
        assert states(env, reader, units) == ["in_progress", "unread", "unread"]
        with env.engine.connect() as connection:
            assert not default_is_read(connection, units[2])  # an admin's revision is not skipped


def test_m1_a_further_page_without_percent_is_the_one_kept(tmp_path: Path) -> None:
    """Same file, same state: the page is never compared, and a NULL percent ranks below any
    percent. Page 190 (KOReader, no percent) loses to page 110 at 55 %."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        volume = env.file("ok", units, bounds=[(1, 100), (101, 200)])
        env.position(reader, volume, page=110, percent=0.55, client_ts=hours(1))
        further = env.position(reader, volume, page=190, client_ts=hours(2))
        assert env.progress(reader)[units[1]].last_position_id == further


def test_m2_percents_of_two_files_are_not_compared_as_one(tmp_path: Path) -> None:
    """The percent is of the file. 60 % of a two-chapter volume is 20 % into chapter 2; 55 % of
    the chapter-2 file is 55 % into it. The volume position wins on the larger number."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        volume = env.file("ok", units)
        chapter = env.file("ok", [units[1]])
        env.position(reader, volume, percent=0.6, client_ts=hours(1))
        further = env.position(reader, chapter, percent=0.55, client_ts=hours(2))
        assert env.progress(reader)[units[1]].last_position_id == further


def test_m2_the_fraction_of_the_unit_decides_not_the_percent_of_the_file(tmp_path: Path) -> None:
    """Two volumes that both hold chapter 2: 60 % of the first is 20 % into it; 30 % of the second
    (where it is the first chapter) is 60 % into it. The larger percent is the lesser position."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        one, two, three = env.units("ok", 3)
        first_volume = env.file("ok", [one, two])
        second_volume = env.file("ok", [two, three])
        env.position(reader, first_volume, percent=0.6, client_ts=hours(2))
        further = env.position(reader, second_volume, percent=0.3, client_ts=hours(1))
        assert env.progress(reader)[two].last_position_id == further


def test_m1_an_exact_current_position_beats_an_approximate_one(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        volume = env.file("ok", units, bounds=[(1, 100), (101, 200)], content_hash=NEW_HASH)
        env.alias(volume, OLD_HASH, NEW_HASH)
        env.position(reader, volume, percent=0.9, content_hash=OLD_HASH, client_ts=hours(1))
        exact = env.position(reader, volume, page=120, client_ts=hours(2))
        assert env.progress(reader)[units[1]].last_position_id == exact


def test_m1_the_latest_position_is_not_the_furthest_one(tmp_path: Path) -> None:
    """`last_position_id` is where the reader got furthest; `latest_position_id` is where the
    reader is: the one a relay must read depends on the question (docs/reference)."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 1)
        item = env.file("ok", units)
        far = env.position(reader, item, percent=0.9, client_ts=hours(1))
        back = env.position(reader, item, percent=0.1, client_ts=hours(5))
        row = env.progress(reader)[units[0]]
        assert (row.last_position_id, row.latest_position_id) == (far, back)


@pytest.mark.parametrize("source", ["server", "device"])
def test_m3_a_replaced_position_never_marks_a_chapter_the_old_file_did_not_hold(
    tmp_path: Path, source: str
) -> None:
    """The volume was read to the end, then re-cut with a chapter inserted in the middle. The old
    position reaches the Units the file covered when it was written, not the new chapter."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        first, inserted, last = env.units("ok", 3)
        volume = env.file("ok", [first, last], content_hash=OLD_HASH)
        with env.engine.begin() as connection:
            if source == "server":
                destination = env.destination()
                env.account(reader, destination)
                record_server_state(
                    connection, user_id=reader, shelf_destination_id=destination,
                    carrier_type="export_item", carrier_id=volume, content_hash=OLD_HASH,
                    locator={}, percent=1.0, client_ts=hours(1),
                )  # fmt: skip
            else:
                record_position(
                    connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
                    carrier_id=volume, content_hash=OLD_HASH, locator={}, percent=1.0,
                    client_ts=hours(1),
                )  # fmt: skip
        env.recut(volume, [first, inserted, last], NEW_HASH)
        got = env.progress(reader)
        assert inserted not in got  # no position of the old file reaches a chapter it never held
        assert got[first].approximate and got[last].approximate


def test_m3_a_position_with_no_remembered_coverage_on_a_replaced_content_reaches_nothing(
    tmp_path: Path,
) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        volume = env.file("ok", units, content_hash=NEW_HASH)
        env.alias(volume, OLD_HASH, NEW_HASH)
        env.position(reader, volume, percent=1.0, content_hash=OLD_HASH, covered=None)
        assert env.progress(reader) == {}


def _record(env, reader: str, volume: str, page: int, hour: float):  # type: ignore[no-untyped-def]
    with env.engine.begin() as connection:
        return record_position(
            connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
            carrier_id=volume, content_hash=NEW_HASH, locator={"page": page}, percent=None,
            client_ts=hours(hour), now=hours(hour), device_id="reader-1",
        )  # fmt: skip


def test_m4_a_peek_at_the_last_page_marks_nothing_read(tmp_path: Path) -> None:
    """A jump to the last page (table of contents, a look at the end) then back to page 3: both
    are positions, and no chapter is read (ADR-0053, MAJ-2)."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 3)
        volume = env.file("ok", units, bounds=[(1, 10), (11, 20), (21, 30)], content_hash=NEW_HASH)
        _record(env, reader, volume, 30, 1)
        _record(env, reader, volume, 3, 2)
        assert [s for s in states(env, reader, units) if s == "read"] == []


def test_m4_a_page_by_page_traversal_marks_read_and_can_be_undone(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 3)
        volume = env.file("ok", units, bounds=[(1, 10), (11, 20), (21, 30)], content_hash=NEW_HASH)
        pages = [1, 3, 5, 7, 9, 10, 11, 13, 15, 17, 19, 20, 21, 23, 25, 27, 29, 30]
        for hour, page in enumerate(pages):
            _record(env, reader, volume, page, hour)
        assert states(env, reader, units) == ["read", "read", "read"]
        with env.engine.begin() as connection:
            assert (
                undo_read_mark(
                    connection, viewer_id=reader, subject_id=reader, unit_id=units[1], now=hours(40)
                )
                == 1
            )
        assert states(env, reader, units) == ["read", "in_progress", "read"]
        _record(env, reader, volume, 30, 41)  # the same jump again does not mark it back
        assert states(env, reader, units)[1] == "in_progress"


def test_m4_a_gap_of_more_than_three_pages_breaks_the_chain(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        volume = env.file("ok", units, bounds=[(1, 10), (11, 20)], content_hash=NEW_HASH)
        # 10 -> 14 skips pages, and at once: not a plausible speed (the speed rule, N3)
        for hour, page in [(0, 1), (1, 4), (2, 7), (3, 10), (3, 14), (4, 17), (5, 20)]:
            _record(env, reader, volume, page, hour)
        assert states(env, reader, units) == ["read", "in_progress"]  # the skipped chapter is not


def test_m4_nobody_undoes_the_mark_of_another_profile(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader, other = env.user("reader"), env.user("other", "admin")
        units = env.units("ok", 1)
        volume = env.file("ok", units, bounds=[(1, 3)], content_hash=NEW_HASH)
        for hour, page in enumerate([1, 2, 3]):
            _record(env, reader, volume, page, hour)
        assert states(env, reader, units) == ["read"]
        with env.engine.begin() as connection:
            assert (
                undo_read_mark(connection, viewer_id=other, subject_id=reader, unit_id=units[0])
                == 0
            )
        assert states(env, reader, units) == ["read"]


def _alias(env, item: str, data: bytes) -> str:  # type: ignore[no-untyped-def]
    file_hash = "sha256:" + hashlib.sha256(data).hexdigest()
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(export.document_alias).values(
                export_item_id=item, file_hash=file_hash, content_hash=file_hash
            )
        )
    return file_hash


def test_m5_the_sweep_hashes_the_bytes_it_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: the file is opened twice, once for SHA-256, once for the sample hashes. A file
    rewritten between the two lends its hashes to the alias of the one that was verified."""
    good, other = b"the file the alias names" * 64, b"a file written since" * 64
    file = tmp_path / "volume.cbz"
    file.write_bytes(good)
    real = render_hooks.koreader_partial_md5  # type: ignore[attr-defined]

    def rewritten_in_between(stream):  # type: ignore[no-untyped-def]
        file.write_bytes(other)  # the export rewrites the file right after the check
        return real(stream)

    monkeypatch.setattr(render_hooks, "koreader_partial_md5", rewritten_in_between)
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        file_hash = _alias(env, item, good)
        heal_alias_hashes(env.engine, locate=lambda *_: file)
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(export.document_alias).where(
                    export.document_alias.c.file_hash == file_hash
                )
            ).one()
        reference = tmp_path / "reference.cbz"
        reference.write_bytes(good)
        assert row.koreader_partial_md5 in (None, "md5:" + koreader_partial_md5(reference))


def test_m6_a_file_known_not_to_match_is_not_hashed_again_at_each_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alias whose file was rewritten in place stays empty forever (correct), but every sweep
    reads the whole file again: a cost that grows with the history of rewrites."""
    file = tmp_path / "volume.cbz"
    file.write_bytes(b"rewritten since" * 4096)
    calls: list[Path] = []
    real = render_hooks._hash_once

    def counted(path: Path, file_hash: str):  # type: ignore[no-untyped-def]
        calls.append(path)
        return real(path, file_hash)

    monkeypatch.setattr(render_hooks, "_hash_once", counted)
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        _alias(env, item, b"an older version of the file")
        heal_alias_hashes(env.engine, locate=lambda *_: file)
        first = len(calls)
        heal_alias_hashes(env.engine, locate=lambda *_: file)
        assert len(calls) == first


# ----------------------------------------------------------------------------------------------
# Second pass (« #284 : second passage »): red again, `xfail` strict until fixed.

RED = pytest.mark.xfail(
    strict=True, reason="#284 second pass: m1, known limit (see docs/decisions/M4.md)"
)


def test_m3_original_attack_a_replaced_position_written_after_the_recut(tmp_path: Path) -> None:
    """The first-pass attack, as written: the old position arrives once the file is re-cut (a
    device that kept the old file, ADR-0053 « l'appareil garde son fichier »). `covered_units` is
    the coverage of the carrier NOW, not of the content the position was on: it reaches the
    inserted chapter."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        first, inserted, last = env.units("ok", 3)
        volume = env.file("ok", [first, inserted, last], content_hash=NEW_HASH)
        env.position(reader, volume, percent=1.0, content_hash=OLD_HASH, client_ts=hours(1))
        assert env.progress(reader)[inserted].state != "read"


@pytest.mark.parametrize("source", ["server", "device"])
def test_m3_through_record_position_after_the_recut(tmp_path: Path, source: str) -> None:
    """The `server` case goes through the internal door (a linked, live account), the `device`
    case through the public service: neither reaches the chapter the old file never held."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        first, inserted, last = env.units("ok", 3)
        volume = env.file("ok", [first, last], content_hash=OLD_HASH)
        env.recut(volume, [first, inserted, last], NEW_HASH)
        with env.engine.begin() as connection:
            if source == "server":
                destination = env.destination()
                env.account(reader, destination)
                record_server_state(
                    connection, user_id=reader, shelf_destination_id=destination,
                    carrier_type="export_item", carrier_id=volume, content_hash=OLD_HASH,
                    locator={}, percent=1.0, client_ts=hours(1),
                )  # fmt: skip
            else:
                record_position(
                    connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
                    carrier_id=volume, content_hash=OLD_HASH, locator={}, percent=1.0,
                    client_ts=hours(1),
                )  # fmt: skip
        assert inserted not in env.progress(reader)


def test_m5_a_same_size_rewrite_that_keeps_its_date_lends_its_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is (size, mtime) seen before and after on the same descriptor. A rewrite in place
    of the same size that keeps the date (`rsync -a`, `cp -p`, `touch -r`) passes it: the sample
    hashes of another file land on the alias that was verified."""
    import os

    good, other = b"G" * 4096, b"O" * 4096
    file = tmp_path / "volume.cbz"
    file.write_bytes(good)
    real = render_hooks.verified_hashes

    def rewritten_in_between(stream):  # type: ignore[no-untyped-def]
        # the same rewrite, once the file is open and before anything is read from it
        seen = os.stat(file)
        file.write_bytes(other)
        os.utime(file, ns=(seen.st_atime_ns, seen.st_mtime_ns))
        return real(stream)

    monkeypatch.setattr(render_hooks, "verified_hashes", rewritten_in_between)
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        file_hash = _alias(env, item, good)
        heal_alias_hashes(env.engine, locate=lambda *_: file)
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(export.document_alias).where(
                    export.document_alias.c.file_hash == file_hash
                )
            ).one()
        reference = tmp_path / "reference.cbz"
        reference.write_bytes(good)
        assert row.koreader_partial_md5 in (None, "md5:" + koreader_partial_md5(reference))


@RED
def test_m1_cache_a_right_file_restored_with_its_date_is_healed(tmp_path: Path) -> None:
    """The cache of files that do not match is keyed by (size, mtime). The right file put back
    with the same size and its date kept (a restore with `rsync -a`) is skipped until restart."""
    import os

    good, wrong = b"G" * 4096, b"W" * 4096
    file = tmp_path / "volume.cbz"
    file.write_bytes(wrong)
    seen = os.stat(file)
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        _alias(env, item, good)
        assert heal_alias_hashes(env.engine, locate=lambda *_: file) == 0
        file.write_bytes(good)
        os.utime(file, ns=(seen.st_atime_ns, seen.st_mtime_ns))
        assert heal_alias_hashes(env.engine, locate=lambda *_: file) == 1


def test_n1_a_reader_cannot_write_a_server_read_state(tmp_path: Path) -> None:
    """`source` is a free argument of the public `record_position`: whoever calls it as a reader
    (a route of L47 that forwards a field, a device token) writes « read » with no traversal."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 3)
        volume = env.file("ok", units, bounds=[(1, 10), (11, 20), (21, 30)], content_hash=NEW_HASH)
        with env.engine.begin() as connection, contextlib.suppress(TypeError):
            # the attack as written: the argument is gone, the call is refused (or, were it
            # still there, nothing may come out as read)
            record_position(
                connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
                carrier_id=volume, content_hash=NEW_HASH, locator={"page": 30}, percent=1.0,
                client_ts=hours(1), source="server",  # type: ignore[call-arg]
            )  # fmt: skip
        assert [s for s in states(env, reader, units) if s == "read"] == []


def test_n2_a_read_state_from_a_server_can_be_undone(tmp_path: Path) -> None:
    """A peek at the last page in Kavita's own reader comes back as a `server` read: the member's
    undo (ADR-0053, MAJ-2: « journal et annulation ») does nothing on it."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 1)
        volume = env.file("ok", units, bounds=[(1, 30)], content_hash=NEW_HASH)
        env.position(reader, volume, page=30, percent=1.0, source="server")
        assert states(env, reader, units) == ["read"]
        with env.engine.begin() as connection:
            undo_read_mark(connection, viewer_id=reader, subject_id=reader, unit_id=units[0])
        assert states(env, reader, units) != ["read"]


def test_n3_the_export_of_a_profile_holds_her_read_marks(tmp_path: Path) -> None:
    """EF-USR-013: the export holds everything that is hers. `read_units`, `undone_units` (the
    journal of what she read and undid) and `source` are not in it."""
    from shelfhold.identity.adapters.store import SqlIdentityStore

    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 1)
        volume = env.file("ok", units, bounds=[(1, 3)], content_hash=NEW_HASH)
        for hour, page in enumerate([1, 2, 3]):
            _record(env, reader, volume, page, hour)
        exported = SqlIdentityStore(env.engine).export_rows(reader, limit=1000)
        assert exported is not None
        columns = set().union(*(dict(r).keys() for r in exported["reading_positions"]))
        assert {"source", "read_units", "undone_units"} <= columns
