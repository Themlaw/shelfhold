"""PR #296 (M4 L47), adversarial review: the gaps of the kosync routes, as red proofs.

Each test is a strict `xfail`: it turns green (and so fails the suite) the day the gap is closed,
and the mark is then removed (docs/decisions/M4.md, "#296 : contradicteur").

What KOReader does with an answer is read in `plugins/kosync.koplugin/` (`KOSyncClient.lua`,
`KOSyncQueue.lua`, `main.lua`) and `frontend/optmath.lua`.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from tests.bundle_env import Chapter
from tests.integration.test_l47_kosync import (
    ABSENT_MD5,
    block,
    library,
    positions,
    push,
    three_chapters,
)
from tests.integration.test_l48_opds import World, make_world

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014")


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


def _koreader_percent(page: int, pages: int) -> float:
    """`Math.roundPercent(current_page / number_of_pages)`: floored to the ten-thousandth."""
    return math.floor(page / pages * 10000) / 10000


@pytest.mark.xfail(strict=True, reason="#296 BLQ-1: 202 is a failure for KOReader")
def test_poc_a_put_that_stores_nothing_is_still_a_200_for_koreader(lab: World) -> None:
    """`KOSyncClient:update_progress` calls back `ok = (res.status == 200)`. Anything else but a 401
    is pushed in `KOSyncQueue`, and `drain` stops at the first failure: one file Shelfhold does not
    know (taken from another feed, above a ceiling, a purged version) heads the queue and holds
    every later progress of the device for up to 4 weeks. The reference server never answers 202."""
    unknown = push(lab.kid, ABSENT_MD5, "5")
    assert unknown.status_code == 200
    assert set(unknown.json()) == {"document", "timestamp"}
    assert positions(lab) == []


@pytest.mark.xfail(strict=True, reason="#296 BLQ-1: a refused integrity guard is a 202")
def test_poc_a_position_the_guard_refuses_does_not_jam_the_queue_of_the_device(lab: World) -> None:
    three_chapters(lab)
    digest, _ = block(lab, lab.mum)
    assert push(lab.mum, digest, "5", 0.1).status_code == 200  # refused, nothing stored, but 200
    assert positions(lab) == []


@pytest.mark.xfail(strict=True, reason="#296 MAJ-1: the chain is cut at each chapter file")
def test_poc_a_block_read_through_marks_the_chapters_it_crossed(lab: World) -> None:
    """ADR-0053 §5: "la chaîne peut franchir plusieurs chapitres". Each page of the block is stored
    on its chapter's file and the traversal only looks at the history of one file: a block read
    from page 1 to page 8 in ten minutes (0.7 page a minute) marks nothing, and a reader who reads a
    block from the first chapter to the last never sees a chapter turn "read" unless the device
    happened to sleep on its last page."""
    items = three_chapters(lab)  # pages 1-3, 4-6, 7-9
    digest, _ = block(lab, lab.mum)
    assert push(lab.mum, digest, "1", _koreader_percent(1, 9)).status_code == 200
    with lab.seed.engine.begin() as c:
        c.execute(
            sa.text("UPDATE reading_position SET server_ts = :t"),
            {"t": (positions(lab)[0].server_ts - timedelta(minutes=10)).replace(tzinfo=None)},
        )
    assert push(lab.mum, digest, "8", _koreader_percent(8, 9)).status_code == 200
    with lab.seed.engine.connect() as c:
        states = dict(
            c.execute(
                sa.text("SELECT unit_id, state FROM reading_progress WHERE state = 'read'")
            ).all()
        )
    assert set(states) == {items[0].unit_id, items[1].unit_id}


@pytest.mark.xfail(strict=True, reason="#296 MIN-1: the guard forgets KOReader floors")
def test_poc_the_first_page_of_a_block_of_159_pages_passes_the_guard(lab: World) -> None:
    """KOReader floors the share to 1/10000 (`Math.roundPercent`): page 1 of 159 is sent as 0.0062,
    so N' = 161.3 and the guard (2 pages) refuses a true position. 3000 pages: 20 of the first 38
    pages refused; beyond 10000 pages, the first pages are sent as 0."""
    lib = library(lab)
    for n in ("1", "2", "3"):
        lib.add(Chapter.simple(n, count=53))
    digest, _ = block(lab, lab.mum)
    assert push(lab.mum, digest, "1", _koreader_percent(1, 159)).status_code == 200
    assert len(positions(lab)) == 1


@pytest.mark.xfail(strict=True, reason="#296 MIN-2: KOReader shows `body.message`")
def test_poc_the_402_of_register_says_why_on_the_device(lab: World) -> None:
    """`KOSync:doRegister` shows `body.message or "Unknown server error"`."""
    answer = lab.mum.client.post(
        "/kosync/users/create",
        json={"username": "x", "password": "y"},
        headers={"accept": "application/vnd.koreader.v1+json"},
    )
    assert answer.status_code == 402
    assert isinstance(answer.json().get("message"), str)
