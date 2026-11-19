"""M5 L09, review of #378: what the reviewer asked for, one test per point.

m1: the spine of an EPUB is read outside the write lock.
m2: a file that cannot be opened for a moment is not remembered as « no end ».
m3: the device of a kosync write is the token's, in a namespace of its own; a reader that says the
    identifier of the browser stays another device.
(C1: `test_m5_l09_contradicteur.py`.)
"""

# ruff: noqa: F401, F811  (the fixtures of the module of L09 are imported to be used here)

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from tests.integration.test_l47_kosync import positions, pull, push, volume
from tests.integration.test_l48_opds import World
from tests.integration.test_m5_l09_kosync_epub import (
    Spines,
    lab,
    second_device,
    spines,
    state,
    walk,
    xp,
)

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014", "ENF-SEC-010")


def test_epub_spine_is_read_outside_the_write_lock(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    path = lab.seed.engine.url.database
    assert path is not None
    held: list[bool] = []

    def probe() -> None:
        """What `epub_spine_of` does is I/O: here, whether another writer could start meanwhile."""
        other = sqlite3.connect(path, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            held.append(False)
        except sqlite3.OperationalError:
            held.append(True)  # the lock is taken: the file is being opened under it
        finally:
            other.close()

    spines.by_item[item.item_id] = 3
    spines.on_ask = probe
    walk(lab, lab.mum, digest, stop=0.96)
    lab.env.clock.advance(timedelta(minutes=2))
    assert push(lab.mum, digest, xp(3, 9000), 1.0, device_id="A1B2C3").status_code == 200
    assert held, "the spine was asked at the end of the book"
    assert held == [False] * len(held), "the file was opened with the write lock held"


def test_epub_unreadable_spine_is_not_remembered_as_no_end(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = None  # the file cannot be opened just now
    walk(lab, lab.mum, digest, stop=0.96)
    lab.env.clock.advance(timedelta(minutes=2))
    assert push(lab.mum, digest, xp(3, 9000), 1.0).status_code == 200
    assert state(lab, item.unit_id) != "read"  # nobody could tell where the book ends
    spines.by_item[item.item_id] = 3  # the file is readable again
    lab.env.clock.advance(timedelta(minutes=2))
    assert push(lab.mum, digest, xp(3, 9000), 1.0).status_code == 200
    assert spines.asked.count(item.item_id) >= 2, "the failure was kept, the file never asked again"
    assert state(lab, item.unit_id) == "read"


def test_epub_device_is_the_token_in_a_namespace_of_its_own(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    assert push(lab.mum, digest, xp(1, 5), 0.3, device_id="browser-1").status_code == 200
    (row,) = [r for r in positions(lab) if r.carrier_id == item.item_id]
    assert row.device_id is not None and row.device_id.startswith("kosync:")
    assert row.device_id != "browser-1" and "browser-1" not in row.device_id
    # what the device said of itself is given back to it, and the server's name never is
    assert pull(lab.mum, digest).json()["device_id"] == "browser-1"


def test_epub_reader_repeating_the_browsers_device_id_is_another_device(
    lab: World,
    spines: Spines,
) -> None:
    """Two tokens that announce one `device_id` are two devices, and neither is the browser."""
    item, digest = volume(lab, count=1, fmt="epub")
    second = second_device(lab, lab.mum)
    assert push(lab.mum, digest, xp(1, 5), 0.3, device_id="browser-1").status_code == 200
    lab.env.clock.advance(timedelta(minutes=2))
    assert push(second, digest, xp(1, 5), 0.3, device_id="browser-1").status_code == 200
    rows = [r for r in positions(lab) if r.carrier_id == item.item_id]
    assert len({r.device_id for r in rows}) == 2
    assert {r.locator["who"] for r in rows} == {r.device_id for r in rows}


def test_epub_kosync_position_never_takes_the_device_id_of_the_web_reader() -> None:
    from shelfhold.reading.application.kosync import _writer

    assert _writer("a-token").startswith("kosync:")
    assert _writer("a-token") == _writer("A-TOKEN")  # a token has no case
    assert _writer("a-token") != _writer("another")


def test_epub_pass_to_kavita_sends_the_device_word_or_a_neutral_name(tmp_path: Path) -> None:
    """What leaves as `device_id` is `client_id` (the device's own word) or a neutral name, and a
    name of the server (the column, or a `client_id` that imitates it) never."""
    from tests.integration.test_m5_l09_kavita_epub import device_xpointer, epub_chapter
    from tests.sync_env import sync_env

    sent: list[object] = []
    for extra in ({"client_id": "KOBO-1"}, {}, {"client_id": "kosync:abcdef0123456789"}):
        with sync_env(tmp_path / str(len(sent))) as env:
            reader = env.member("reader")
            chapter = epub_chapter(env)
            device_xpointer(
                env, reader, chapter, 0.5, device="kosync:0123456789abcdef", extra=extra
            )
            assert env.sync(reader).pushed == 1
            (write,) = env.fake.kosync_writes
            sent.append(write["body"].get("device_id"))
    assert sent == ["KOBO-1", "shelfhold", "shelfhold"]
