"""M5 L09 (EF-LEC-009, EF-LEC-014, ENF-SEC-010): an EPUB through kosync.

What KOReader sends for a reflowable book is an xpointer. It is kept as the device wrote it (an
opaque locator, never run, never turned into a page), its document is read with bounds
(`reading.domain.epub_locator`), and a Unit is read **only** when the last document of the spine is
reached at its end by a plausible walk **of that device**. A position of one writer is never the
proof for another. The attacks: a child above her ceiling, another profile, a revoked device, an
expired token. Real requests on the test application; the spine comes from a seam the tests own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from tests.integration.test_l47_kosync import XPOINTER, positions, pull, push, volume
from tests.integration.test_l48_opds import Reader, World, auth_of, make_world

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014", "ENF-SEC-010")


class Spines:
    """The seam of `shelfhold.main.epub_spine_of`: how many documents the spine of a file has."""

    def __init__(self) -> None:
        self.by_item: dict[str, int | None] = {}
        self.asked: list[str] = []

    def factory(self, _access: Callable[[], Any]) -> Callable[[str], int | None]:
        def spine_of(item_id: str) -> int | None:
            self.asked.append(item_id)
            return self.by_item.get(item_id)

        return spine_of


@pytest.fixture
def spines(monkeypatch: pytest.MonkeyPatch) -> Spines:
    seam = Spines()
    monkeypatch.setattr("shelfhold.main.epub_spine_of", seam.factory)
    return seam


@pytest.fixture
def lab(spines: Spines, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


def xp(fragment: int, n: int = 1) -> str:
    return f"/body/DocFragment[{fragment}]/body/div[2]/p[12]/text().{n}"


def at(share: float, documents: int = 3) -> str:
    """Where a reader is at `share` of a book of `documents` equal documents."""
    return xp(min(documents, 1 + int(share * documents)), int(share * 1000))


def walk(
    lab: World, reader: Reader, digest: str, *, start: float = 0.04, stop: float = 1.0,
    documents: int = 3, device_id: str = "A1B2C3", step: float = 0.04,
) -> list[int]:  # fmt: skip
    """A plausible read: 4 % at a time, two minutes apart (a minute is a window of one position)."""
    codes, share = [], start
    while share <= stop + 1e-9:
        lab.env.clock.advance(timedelta(minutes=2))
        answer = push(reader, digest, at(share, documents), min(share, 1.0), device_id=device_id)
        codes.append(answer.status_code)
        share += step
    return codes


def state(lab: World, unit: str) -> str:
    with lab.seed.engine.connect() as c:
        found = (
            c.execute(sa.text("SELECT state FROM reading_progress WHERE unit_id = :u"), {"u": unit})
            .scalars()
            .all()
        )
    return found[0] if found else "unread"


def marks(lab: World) -> int:
    with lab.seed.engine.connect() as c:
        return int(
            c.execute(
                sa.text(
                    "SELECT count(*) FROM reading_position WHERE json_array_length(read_units) > 0"
                )
            ).scalar_one()
        )


# -- the xpointer is opaque, its document is read ---------------------------------------------


def test_epub_xpointer_is_kept_opaque_and_its_spine_index_is_read(lab: World) -> None:
    _item, digest = volume(lab, count=1, fmt="epub")
    assert push(lab.mum, digest, XPOINTER, 0.3).status_code == 200
    (row,) = positions(lab)
    assert row.locator["xpointer"] == XPOINTER  # as the device wrote it, to the character
    assert row.locator["spine"] == 2 and "page" not in row.locator
    assert pull(lab.mum, digest).json()["progress"] == XPOINTER  # given back as it came


def test_epub_xpointer_off_grammar_is_kept_opaque_and_gives_no_index(lab: World) -> None:
    _item, digest = volume(lab, count=1, fmt="epub")
    odd = "/body/DocFragment[0]/body/p"  # not an index, but still a place a device chose to name
    assert push(lab.mum, digest, odd, 0.3).status_code == 200
    (row,) = positions(lab)
    assert row.locator["xpointer"] == odd and "spine" not in row.locator
    assert pull(lab.mum, digest).json()["progress"] == odd


# -- read only at the end of the last document of the spine ------------------------------------


def test_epub_read_only_at_end_of_spine(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    assert set(walk(lab, lab.mum, digest, stop=0.96)) == {
        200
    }  # in the last document, not at its end
    assert state(lab, item.unit_id) != "read" and marks(lab) == 0
    lab.env.clock.advance(timedelta(minutes=2))
    assert push(lab.mum, digest, xp(3, 9000), 1.0, device_id="A1B2C3").status_code == 200
    assert state(lab, item.unit_id) == "read" and marks(lab) == 1
    assert pull(lab.mum, digest).json()["progress"] == xp(3, 9000)  # given back, as it came


def test_epub_not_read_when_the_spine_is_longer_than_the_last_document_reached(
    lab: World, spines: Spines
) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = (
        5  # the device is in document 3 of 5, at "100 %" of what it knows
    )
    walk(lab, lab.mum, digest, stop=1.0, documents=3)
    assert state(lab, item.unit_id) != "read" and marks(lab) == 0


def test_epub_not_read_when_no_spine_can_be_told(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = None  # the file cannot be opened, or has no reading order
    walk(lab, lab.mum, digest, stop=1.0)
    assert state(lab, item.unit_id) != "read" and marks(lab) == 0


def test_epub_a_leap_to_the_end_is_not_a_reading(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    assert push(lab.mum, digest, at(0.04), 0.04).status_code == 200
    assert push(lab.mum, digest, xp(3, 9000), 1.0).status_code == 200  # the same second
    assert state(lab, item.unit_id) != "read" and marks(lab) == 0
    lab.env.clock.advance(timedelta(minutes=2))  # nor two minutes later: 96 % in two minutes
    assert push(lab.mum, digest, xp(3, 9001), 1.0).status_code == 200
    assert state(lab, item.unit_id) != "read" and marks(lab) == 0


def test_epub_the_spine_is_asked_only_for_a_position_that_may_be_the_end(
    lab: World, spines: Spines
) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    walk(lab, lab.mum, digest, stop=0.9)
    assert spines.asked == []  # the file is not opened for a place in the middle of a book


# -- never the proof of another writer ----------------------------------------------------------


def test_epub_position_not_proof_of_other_writer(lab: World, spines: Spines) -> None:
    """MIN-6: the positions of one device make a walk for that device only. Another device that
    arrives at the end with nothing behind it has read nothing, and the history of the first is
    not a step of its chain."""
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    walk(lab, lab.mum, digest, stop=0.96, device_id="TABLET")  # a long read, on the tablet
    assert state(lab, item.unit_id) != "read"
    lab.env.clock.advance(timedelta(minutes=2))
    # a phone shows up at the end of the book: it has said nothing before
    assert push(lab.mum, digest, xp(3, 9000), 1.0, device_id="PHONE").status_code == 200
    assert state(lab, item.unit_id) != "read" and marks(lab) == 0
    # and the other way: the tablet's own walk is still its own
    lab.env.clock.advance(timedelta(minutes=2))
    assert push(lab.mum, digest, xp(3, 9000), 1.0, device_id="TABLET").status_code == 200
    assert state(lab, item.unit_id) == "read"


def test_epub_positions_without_a_device_id_are_one_writer_per_token(
    lab: World, spines: Spines
) -> None:
    """A client that sends no `device_id` is told apart by its token alone: a named device does not
    borrow its history."""
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    for share in (0.2, 0.4, 0.6, 0.8, 0.92):
        lab.env.clock.advance(timedelta(minutes=2))
        assert push(lab.mum, digest, at(share), share, device_id=None).status_code == 200
    lab.env.clock.advance(timedelta(minutes=2))
    push(lab.mum, digest, xp(3, 9000), 1.0, device_id="NAMED")
    assert state(lab, item.unit_id) != "read"


# -- the attacks ---------------------------------------------------------------------------------


def test_epub_child_above_her_ceiling_writes_nothing_and_opens_no_file(
    lab: World, spines: Spines
) -> None:
    hidden, digest = volume(lab, key="above", count=1, fmt="epub")
    spines.by_item[hidden.item_id] = 3
    assert push(lab.mum, digest, at(0.04), 0.04).status_code == 200  # an adult's own place on it
    mine = len(positions(lab))
    spines.asked.clear()
    for share in (0.2, 0.5, 0.9):
        lab.env.clock.advance(timedelta(minutes=2))
        assert push(lab.kid, digest, at(share), share).status_code == 200  # the answer of nothing
    lab.env.clock.advance(timedelta(minutes=2))
    assert push(lab.kid, digest, xp(3, 9000), 1.0).status_code == 200
    assert len(positions(lab)) == mine  # not one row for her
    assert spines.asked == []  # the file of a Work she cannot see is not even opened
    assert pull(lab.kid, digest).json() == {}  # and nothing of the adult's place comes back
    assert state(lab, hidden.unit_id) != "read"


def test_epub_another_profile_neither_reads_nor_completes_my_walk(
    lab: World, spines: Spines
) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    walk(lab, lab.mum, digest, stop=0.92, device_id="SAME")
    lab.env.clock.advance(timedelta(minutes=2))
    # the child sends the end of the book from a device with the very same identifier
    assert push(lab.kid, digest, xp(3, 9000), 1.0, device_id="SAME").status_code == 200
    with lab.seed.engine.connect() as c:
        kid_marks = c.execute(
            sa.text(
                "SELECT count(*) FROM reading_position p JOIN user u ON u.id = p.user_id "
                "WHERE u.username = 'kid' AND json_array_length(p.read_units) > 0"
            )
        ).scalar_one()
    assert kid_marks == 0  # her chain is hers: one point
    assert pull(lab.kid, digest).json()["progress"] == xp(3, 9000)
    assert pull(lab.mum, digest).json()["progress"] == at(0.92)  # nothing of hers is mum's


def test_epub_a_revoked_device_writes_nothing_and_ends_no_book(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    walk(lab, lab.mum, digest, stop=0.92)
    assert auth_of(lab.env).revoke_device_token(lab.mum.public_id) is True
    lab.env.clock.advance(timedelta(minutes=2))
    before = len(positions(lab))
    spines.asked.clear()
    assert push(lab.mum, digest, xp(3, 9000), 1.0).status_code == 401
    assert pull(lab.mum, digest).status_code == 401
    assert len(positions(lab)) == before and spines.asked == []
    assert state(lab, item.unit_id) != "read"


def test_epub_an_expired_token_writes_nothing_and_ends_no_book(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    walk(lab, lab.mum, digest, stop=0.92)
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE device_token SET expires_at = '2000-01-01 00:00:00' WHERE public_id = :p"
            ),
            {"p": lab.mum.public_id.lower()},
        )
    lab.env.clock.advance(timedelta(minutes=2))
    before = len(positions(lab))
    spines.asked.clear()
    assert push(lab.mum, digest, xp(3, 9000), 1.0).status_code == 401
    assert len(positions(lab)) == before and spines.asked == []
    assert state(lab, item.unit_id) != "read"


def test_epub_a_forged_spine_or_end_in_the_body_changes_nothing(lab: World, spines: Spines) -> None:
    """The index and the end are the server's: a body that carries them is no more than a body."""
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    assert (
        push(
            lab.mum, digest, xp(1), 0.1, spine=99, end=True, who="x", locator={"end": True}
        ).status_code
        == 200
    )
    (row,) = positions(lab)
    assert row.locator["spine"] == 0 and "end" not in row.locator
    assert row.locator.get("who") != "x"
    assert state(lab, item.unit_id) != "read"


def test_epub_a_leap_is_no_read_even_when_the_book_has_a_page_count(
    lab: World, spines: Spines
) -> None:
    """The page count of an EPUB is its number of sections: it is no speed to judge a share by."""
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 30
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE content SET page_count = 30 "
                "WHERE id = (SELECT content_id FROM export_item WHERE id = :i)"
            ),
            {"i": item.item_id},
        )
    assert push(lab.mum, digest, at(0.04, 30), 0.04, device_id="A").status_code == 200
    lab.env.clock.advance(timedelta(minutes=2))
    assert push(lab.mum, digest, xp(30, 9000), 1.0, device_id="A").status_code == 200
    assert state(lab, item.unit_id) != "read" and marks(lab) == 0


def test_epub_the_spine_of_a_file_is_opened_once(lab: World, spines: Spines) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    for _ in range(5):
        lab.env.clock.advance(timedelta(minutes=2))
        push(lab.mum, digest, xp(3, 9000), 1.0, device_id="A")
    assert spines.asked == [item.item_id]


def test_epub_two_tokens_repeating_one_device_id_are_two_writers(
    lab: World, spines: Spines
) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    spines.by_item[item.item_id] = 3
    walk(lab, lab.mum, digest, stop=0.96, device_id="CLONE")
    lab.env.clock.advance(timedelta(minutes=2))
    mine = {r.locator["who"] for r in positions(lab)}
    push(lab.kid, digest, xp(3, 9000), 1.0, device_id="CLONE")
    assert len(mine) == 1 and state(lab, item.unit_id) != "read"
