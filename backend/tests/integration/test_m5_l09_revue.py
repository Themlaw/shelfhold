"""M5 L09, relecture adverse de #378 : ce que la revue a demandé, un test par point.

m1 : le spine d'un EPUB est lu hors du verrou d'écriture.
m2 : un fichier momentanément illisible n'est pas retenu comme « sans fin ».
m3 : l'appareil d'une écriture kosync est celui du jeton, dans un espace de noms à part ; un
     lecteur qui annonce l'identifiant du navigateur reste un autre appareil.
(C1 : `test_m5_l09_contradicteur.py`.)
"""

# ruff: noqa: F401, F811  (the fixtures of the module of L09 are imported to be used here)

from __future__ import annotations

import sqlite3
from datetime import timedelta

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
