"""PR #336 (M4 L26), relecture adverse : un `.tachibk` lu comme hostile, et fidèle au format.

Each test is the proof of a hole found in review, red in strict xfail until it is closed. The
backups are built here by hand from the public models of the format (`Backup`, `BackupManga`,
`BackupChapter`, `BackupHistory`), not from what the decoder reads.

* C1: a top-level field the decoder does not know refuses the whole file. Protobuf adds fields, it
  never changes the meaning of a known one; Mihon ignores what it does not know. The forks that
  write the same format add their own top-level fields (TachiyomiSY and the forks built on it:
  600 saved searches, 601 feeds), so their backups, and the next field Mihon adds, are refused.
* C2: the entry budget counts every length-delimited field, strings included. A real chapter
  carries its address, its name and its team (four entries) and a read one its history (two
  more): a library of 200 series of 100 read chapters, an ordinary one, is refused as too large.
"""

from __future__ import annotations

import struct

import pytest

from shelfhold.platform.import_tachibk.decode import TachibkRefusedError, decode_backup
from shelfhold.platform.import_tachibk.map import map_backup
from tests import tachibk_env as env

pytestmark = pytest.mark.req("EF-INT-008")


def _series(source: int, n: int, chapters: int) -> bytes:
    """A `BackupManga` as Mihon writes it: the fields it fills for a series of a library."""
    body = env.vi(1, source) + env.ld(2, f"/manga/{n}") + env.ld(3, f"Series {n}")
    body += env.ld(4, "An artist") + env.ld(5, "An author") + env.ld(6, "A description")
    body += b"".join(env.ld(7, genre) for genre in ("Action", "Drama", "Fantasy"))
    body += env.ld(9, f"https://covers.example/{n}.jpg")
    for c in range(chapters):  # BackupChapter: url, name, scanlator, read, chapterNumber
        chapter = env.ld(1, f"/chapter/{n}/{c}") + env.ld(2, f"Chapter {c}") + env.ld(3, "Team")
        chapter += env.vi(4, 1) + env.tag(9, 5) + struct.pack("<f", float(c + 1))
        body += env.ld(16, chapter)
    for c in range(chapters):  # BackupHistory: url, lastRead, readDuration
        history = env.ld(1, f"/chapter/{n}/{c}") + env.vi(2, 1_700_000_000_000) + env.vi(3, 60_000)
        body += env.ld(104, history)
    return body


def _sources() -> bytes:
    return env.ld(101, env.ld(1, env.SOURCE_NAME) + env.vi(2, env.SOURCE_ID))


@pytest.mark.xfail(
    strict=True, raises=TachibkRefusedError,
    reason="C1: a top-level field this build does not know refuses the file (unknown_schema)",
)  # fmt: skip
@pytest.mark.parametrize(
    "later",
    [
        env.ld(600, env.ld(1, "a saved search")) + env.ld(601, env.vi(1, 1)),  # TachiyomiSY
        env.ld(107, b""),  # the next field Mihon adds
    ],
    ids=["fork-saved-searches-and-feeds", "mihon-next-field"],
)
def test_poc_a_backup_with_a_later_top_level_field_is_read(later: bytes) -> None:
    message = env.ld(1, _series(env.SOURCE_ID, 1, 3)) + _sources() + later
    staged = map_backup(decode_backup(env.tachibk(message)))
    assert len(staged.series) == 1 and staged.read == 3


@pytest.mark.xfail(
    strict=True, raises=TachibkRefusedError,
    reason="C2: strings count as entries: 20 000 read chapters are refused as too_many_entries",
)  # fmt: skip
def test_poc_an_ordinary_library_of_twenty_thousand_read_chapters_is_read() -> None:
    message = b"".join(env.ld(1, _series(env.SOURCE_ID, n, 100)) for n in range(200)) + _sources()
    backup = decode_backup(env.tachibk(message))
    assert sum(len(m.chapters) for m in backup.manga) == 20_000
