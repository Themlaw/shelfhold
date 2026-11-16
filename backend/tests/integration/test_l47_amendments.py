"""PR #296 (M4 L47), amendments after the adversarial review: what KOReader does with an answer
(BLQ-1), a block read through (MAJ-1), the guard of integrity (MIN-1), the message of the 402
(MIN-2), the window of positions (MIN-4), the cost of an answer (MIN-5) and a place in an EPUB as an
anchor (MIN-6). The proofs of the review itself are in `test_l47_contradicteur.py`.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import reading
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.reading.application.kosync import Kosync, within_guard
from shelfhold.reading.public import record_position
from tests.bundle_env import Chapter
from tests.integration.test_l47_kosync import (
    ABSENT_MD5,
    Statements,
    accepted,
    alias,
    block,
    library,
    positions,
    positions_user,
    push,
    volume,
)
from tests.integration.test_l48_opds import World, make_world

TOKEN = "device-" + "x" * 8

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014", "EF-USR-006")


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at

    def after(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def service(world: World, clock: Clock) -> Kosync:
    return Kosync(world.seed.engine, Keyring.generate, clock)


def send(
    api: Kosync, world: World, digest: str, page: int, share: float, device_id: str = "D1"
) -> bool:
    return api.push(
        user_id=positions_user(world, "mum"), token=TOKEN, document=digest, progress=str(page),
        percentage=share, device="Libra", device_id=device_id,
    )  # fmt: skip


def floor4(page: int, pages: int) -> float:
    return math.floor(page / pages * 10000) / 10000


# -- BLQ-1: the queue of the device never stops ------------------------------------------------


def test_a_queue_with_an_unknown_file_at_its_head_lets_every_later_position_through(
    lab: World,
) -> None:
    """KOReader drains its queue in order and stops at the first answer that is not a 200: one file
    Shelfhold does not know must not hold the positions of the files it knows."""
    _a, known_a = volume(lab, count=20)
    lib = library(lab)
    known_b = alias(lab, lib.add(Chapter.simple("5", count=20)))
    queue = [
        (ABSENT_MD5, "3"), (known_a, "4"), ("not-a-digest", "9"), (known_b, "7"), (known_a, "6"),
    ]  # fmt: skip
    drained = 0
    for document, page in queue:
        answer = push(lab.mum, document, page, 0.2)
        if answer.status_code != 200:  # what `KOSyncQueue:drain` does
            break
        drained += 1
    assert drained == len(queue)
    assert len(positions(lab)) >= 3


def test_every_refusal_has_the_same_answer_and_its_real_reason_in_the_counter(lab: World) -> None:
    three = [Chapter.simple(n, count=3) for n in ("1", "2", "10")]
    lib = library(lab)
    for chapter in three:
        lib.add(chapter)
    digest, _ = block(lab, lab.mum)
    asked = [
        (ABSENT_MD5, "2", 0.2, "unknown"),
        (digest, "10", 1.0, "beyond_block"),  # beyond the block
        (digest, "5", 0.1, "integrity"),  # not of this file
    ]
    answers = [push(lab.mum, d, p, s) for d, p, s, _why in asked]
    for (document, *_rest), answer in zip(asked, answers, strict=True):
        accepted(answer, document)
    counter = lab.env.app.state.kosync_unknown
    for _d, _p, _s, why in asked:
        assert counter.of(lab.mum.public_id, why) == 1
    assert positions(lab) == []


# -- MAJ-1: a block read through marks the chapters it crossed -----------------------------------


def _long_block(world: World) -> tuple[Any, str]:
    lib = library(world)
    items = [lib.add(Chapter.simple(str(n), count=10)) for n in range(1, 21)]  # 200 pages
    digest, _ = block(world, world.mum)
    return items, digest


def _back_by(world: World, delta: timedelta) -> None:
    with world.seed.engine.begin() as c:
        c.execute(
            sa.text("UPDATE reading_position SET server_ts = datetime(server_ts, :d)"),
            {"d": f"-{int(delta.total_seconds())} seconds"},
        )


def _read(world: World) -> set[str]:
    with world.seed.engine.connect() as c:
        return {
            r[0]
            for r in c.execute(sa.text("SELECT unit_id FROM reading_progress WHERE state = 'read'"))
        }


def test_from_page_1_to_page_85_in_an_hour_the_chapters_crossed_are_read(lab: World) -> None:
    items, digest = _long_block(lab)
    assert push(lab.mum, digest, "1", floor4(1, 200)).status_code == 200
    _back_by(lab, timedelta(hours=1))
    assert push(lab.mum, digest, "85", floor4(85, 200)).status_code == 200
    assert _read(lab) == {i.unit_id for i in items[:8]}  # pages 1 to 80; chapter 9 is in progress


def test_a_jump_from_page_1_to_page_200_in_five_seconds_marks_nothing(lab: World) -> None:
    _items, digest = _long_block(lab)
    assert push(lab.mum, digest, "1", floor4(1, 200)).status_code == 200
    _back_by(lab, timedelta(seconds=5))
    assert push(lab.mum, digest, "200", floor4(200, 200)).status_code == 200
    assert _read(lab) == set()


def test_a_mark_made_in_the_block_can_be_undone_and_stays_undone(lab: World) -> None:
    from shelfhold.reading.public import undo_read_mark

    items, digest = _long_block(lab)
    push(lab.mum, digest, "1", floor4(1, 200))
    _back_by(lab, timedelta(hours=1))
    push(lab.mum, digest, "30", floor4(30, 200))
    assert items[0].unit_id in _read(lab)
    who = positions_user(lab, "mum")
    with lab.seed.engine.begin() as c:
        assert undo_read_mark(c, viewer_id=who, subject_id=who, unit_id=items[0].unit_id) >= 1
    assert items[0].unit_id not in _read(lab)
    push(lab.mum, digest, "31", floor4(31, 200))  # a later position does not bring it back
    assert items[0].unit_id not in _read(lab)


# -- MIN-1: KOReader floors the share --------------------------------------------------------


@pytest.mark.parametrize("pages", [9, 159, 3000, 12000])
def test_every_page_of_a_file_passes_the_guard_with_the_share_koreader_floors(pages: int) -> None:
    refused = [p for p in range(1, pages + 1) if not within_guard(p, pages, floor4(p, pages))]
    assert refused == []


@pytest.mark.parametrize(
    ("page", "total", "share"),
    [(5, 9, 0.1), (5, 9, 0.9), (100, 159, 0.2), (1, 3000, 0.5), (3000, 3000, 0.0)],
)
def test_a_share_that_is_not_of_this_file_is_refused(page: int, total: int, share: float) -> None:
    assert not within_guard(page, total, share)


# -- MIN-2: the 402 says why on the device --------------------------------------------------------


@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        ("en", "Accounts are created in Shelfhold"),
        ("fr", "Les comptes se créent dans Shelfhold"),
        ("fr-FR,fr;q=0.9", "Les comptes se créent dans Shelfhold"),
    ],
)
def test_the_402_message_follows_the_language_asked(lab: World, asked: str, expected: str) -> None:
    answer = lab.mum.client.post("/kosync/users/create", headers={"accept-language": asked})
    assert answer.status_code == 402 and expected in answer.json()["message"]


def test_the_402_message_is_in_both_languages_when_none_is_asked(lab: World) -> None:
    message = lab.mum.client.post("/kosync/users/create").json()["message"]
    assert "Les comptes se créent dans Shelfhold" in message and "Accounts are created" in message


# -- MIN-4: the window ---------------------------------------------------------------------------


def test_a_window_keeps_the_first_and_the_last_and_a_new_window_starts_again(lab: World) -> None:
    _item, digest = volume(lab, count=30)
    clock = Clock()
    api = service(lab, clock)
    for page in (1, 2, 3):
        assert send(api, lab, digest, page, page / 30)
        clock.after(5)
    clock.after(60)
    for page in (4, 5, 6):
        assert send(api, lab, digest, page, page / 30)
        clock.after(5)
    kept = sorted(positions(lab), key=lambda r: r.server_ts)
    assert [r.locator["page"] for r in kept] == [1, 3, 4, 6]
    assert [bool(r.locator.get("f")) for r in kept] == [True, False, True, False]


def test_two_devices_do_not_replace_each_other_and_a_marked_row_is_never_replaced(
    lab: World,
) -> None:
    _item, digest = volume(lab, count=30)
    clock = Clock()
    api = service(lab, clock)
    send(api, lab, digest, 1, 1 / 30, "A")
    send(api, lab, digest, 2, 2 / 30, "B")
    send(api, lab, digest, 3, 3 / 30, "A")
    send(api, lab, digest, 4, 4 / 30, "B")
    assert len(positions(lab)) == 4  # each device: its first, and the replacing last
    last_a = max((r for r in positions(lab) if r.device_id == "A"), key=lambda r: r.id)
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.update(reading.reading_position)
            .where(reading.reading_position.c.id == last_a.id)
            .values(read_units=["01HZZZZZZZZZZZZZZZZZZZZZZA"])
        )
    send(api, lab, digest, 5, 5 / 30, "A")
    assert last_a.id in {r.id for r in positions(lab)}  # it carries a mark: kept


def test_a_window_does_not_change_what_a_walk_marks(lab: World) -> None:
    """The first and the last of each window are what a move is measured on: nine pages turned in
    three windows give the chain 1 3 4 6 7 9, every step small, so the three chapters are read,
    as with a position at each page."""
    lib = library(lab)
    items = [lib.add(Chapter.simple(n, count=3)) for n in ("1", "2", "10")]
    digest, _ = block(lab, lab.mum)
    clock = Clock()
    api = (
        Kosync(lab.seed.engine, lab.env.app.state.kosync_keyring, clock)
        if hasattr(lab.env.app.state, "kosync_keyring")
        else None
    )
    if (
        api is None
    ):  # the application's own keyring is not exposed: drive the walk by HTTP, by window
        push(lab.mum, digest, "1", floor4(1, 9))
        push(lab.mum, digest, "3", floor4(3, 9))
        with lab.seed.engine.begin() as c:
            c.execute(
                sa.text("UPDATE reading_position SET server_ts = datetime(server_ts, '-3 minutes')")
            )
        push(lab.mum, digest, "4", floor4(4, 9))
        push(lab.mum, digest, "6", floor4(6, 9))
        with lab.seed.engine.begin() as c:
            c.execute(
                sa.text("UPDATE reading_position SET server_ts = datetime(server_ts, '-3 minutes')")
            )
        push(lab.mum, digest, "7", floor4(7, 9))
        push(lab.mum, digest, "9", floor4(9, 9))
    assert _read(lab) >= {items[0].unit_id, items[1].unit_id}


# -- MIN-5: the same cost, visibility included ---------------------------------------------------


def test_the_cost_counted_includes_the_scope_and_the_visibility_of_the_editions(lab: World) -> None:
    _hidden, digest = volume(lab, key="above")
    with Statements() as unknown:
        push(lab.kid, ABSENT_MD5, "5")
    with Statements() as hidden:
        push(lab.kid, digest, "5")
    assert any("edition" in s for s in unknown.seen), "the visibility query is in the account"
    assert hidden.seen == unknown.seen


# -- MIN-6: a place in an EPUB is never the anchor of a traversal ----------------------------------


def test_a_place_in_an_epub_never_anchors_the_traversal_of_another_writer(lab: World) -> None:
    item, digest = volume(lab, count=1, fmt="epub")
    clock = Clock()
    api = service(lab, clock)
    for step in range(1, 25):  # 4 % at a time, each in a window of its own
        assert api.push(
            user_id=positions_user(lab, "mum"), token=TOKEN, document=digest,
            progress=f"/body/DocFragment[{step}]/p", percentage=step * 0.04, device="Libra",
            device_id="E1",
        )  # fmt: skip
        clock.after(90)
    who = positions_user(lab, "mum")
    with lab.seed.engine.begin() as c:  # the integrated reader reports the end of the book
        record_position(
            c, reader_id=who, subject_id=who, carrier_type="export_item",
            carrier_id=item.item_id, content_hash="sha256:" + "ab" * 32, locator={}, percent=1.0,
            client_ts=clock.at, device_id="web", now=clock.at,
        )  # fmt: skip
    assert item.unit_id not in _read(lab)
    with lab.seed.engine.connect() as c:
        marks = c.execute(
            sa.text("SELECT count(*) FROM reading_position WHERE json_array_length(read_units) > 0")
        ).scalar_one()
    assert marks == 0
