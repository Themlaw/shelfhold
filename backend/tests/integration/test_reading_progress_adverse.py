"""Adversarial review of #284 (M4 L14a, merged `reading_progress`): proofs of concept, red on
purpose (`xfail` strict until fixed: the fix makes them pass, then the marker goes).

Each test states what the merge rule or the alias sweep should do and fails on
`feat/m4-l14a-reading-progress` as it stands (see `docs/decisions/M4.md`, « #284 : contradicteur »).
The production code is not touched here.
"""

import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.catalog.application.revisions import default_is_read
from shelfhold.export.application import render_hooks
from shelfhold.export.application.render_hooks import heal_alias_hashes
from shelfhold.infrastructure.schema import export
from shelfhold.reading.domain.document_hash import koreader_partial_md5
from shelfhold.reading.public import record_position
from tests.reading_env import NEW_HASH, OLD_HASH, hours, reading_env

pytestmark = pytest.mark.req("EF-LEC-014")

RED = pytest.mark.xfail(strict=True, reason="#284 contradicteur: open objection")


def states(env, user: str, units: list[str]) -> list[str]:  # type: ignore[no-untyped-def]
    got = env.progress(user)
    return [got[u].state if u in got else "-" for u in units]


@RED
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


@RED
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


@RED
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


@RED
def test_m3_a_replaced_position_never_marks_a_chapter_the_old_file_did_not_hold(
    tmp_path: Path,
) -> None:
    """The volume was read to the end, then re-cut with a chapter inserted in the middle. The old
    position (old hash, 100 %) is re-projected on today's coverage and marks the new chapter
    read: a chapter no position reached (ADR-0053), and it blocks its revision for everyone."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        first, inserted, last = env.units("ok", 3)
        volume = env.file("ok", [first, inserted, last], content_hash=NEW_HASH)
        env.position(reader, volume, percent=1.0, content_hash=OLD_HASH, client_ts=hours(1))
        assert env.progress(reader)[inserted].state != "read"


@RED
def test_m4_a_peek_at_the_last_page_does_not_read_the_volume_for_good(tmp_path: Path) -> None:
    """A jump to the last page (table of contents, a look at the end) then back to page 3: every
    chapter skipped is read, never to go back (the merge is sticky), with no undo. ADR-0053
    (MAJ-2, accepted) asks for a traversal before the chapters before are marked read."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 3)
        volume = env.file("ok", units, bounds=[(1, 10), (11, 20), (21, 30)])
        env.position(reader, volume, page=30, percent=1.0, client_ts=hours(1))
        env.position(reader, volume, page=3, percent=0.1, client_ts=hours(2))
        assert env.progress(reader)[units[1]].state != "read"


def _alias(env, item: str, data: bytes) -> str:  # type: ignore[no-untyped-def]
    file_hash = "sha256:" + hashlib.sha256(data).hexdigest()
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(export.document_alias).values(
                export_item_id=item, file_hash=file_hash, content_hash=file_hash
            )
        )
    return file_hash


@RED
def test_m5_the_sweep_hashes_the_bytes_it_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: the file is opened twice, once for SHA-256, once for the sample hashes. A file
    rewritten between the two lends its hashes to the alias of the one that was verified."""
    good, other = b"the file the alias names" * 64, b"a file written since" * 64
    file = tmp_path / "volume.cbz"
    file.write_bytes(good)
    real = render_hooks.sample_hashes

    def rewritten_in_between(path: Path):  # type: ignore[no-untyped-def]
        path.write_bytes(other)  # the export rewrites the file right after the check
        return real(path)

    monkeypatch.setattr(render_hooks, "sample_hashes", rewritten_in_between)
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
def test_m6_a_file_known_not_to_match_is_not_hashed_again_at_each_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alias whose file was rewritten in place stays empty forever (correct), but every sweep
    reads the whole file again: a cost that grows with the history of rewrites."""
    file = tmp_path / "volume.cbz"
    file.write_bytes(b"rewritten since" * 4096)
    calls: list[Path] = []
    real = render_hooks._sha256

    def counted(path: Path) -> str:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(render_hooks, "_sha256", counted)
    with reading_env(tmp_path) as env:
        item = env.file("ok", env.units("ok", 1))
        _alias(env, item, b"an older version of the file")
        heal_alias_hashes(env.engine, locate=lambda *_: file)
        first = len(calls)
        heal_alias_hashes(env.engine, locate=lambda *_: file)
        assert len(calls) == first
