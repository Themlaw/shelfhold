"""PR #301 (M4 L50): the adversarial review's proofs, against the whole chain.

Each case is a hole found on the head of the PR and is kept red (`xfail(strict=True)`): when the
hole is closed, the case turns green and its marker goes with the fix. The production code is not
touched here. Details in `docs/decisions/M4.md`, « #301 : contradicteur ».
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from tests.bundle_env import Chapter
from tests.integration.test_l47_amendments import _back_by, floor4
from tests.integration.test_l47_kosync import block, push
from tests.integration.test_m4_l50_relay import PAGES, lab, progress_of, put, three
from tests.relay_env import build_relay

__all__ = ["lab"]  # the fixture, shared with the relay's own tests

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014", "EF-EXP-026", "EF-USR-006")


@pytest.mark.xfail(
    strict=True,
    reason="B1: the page of Kavita given back by a GET, echoed by KOReader's next PUT, is taken "
    "for a reading of the chapters between, and those are pushed « read » to Kavita",
)
def test_the_echo_of_a_kavita_page_given_by_a_get_never_pushes_a_skipped_chapter_read(
    lab: Any,
) -> None:
    relay, items, digest = three(lab)
    put(lab, digest, 5)  # the e-reader: chapter 1, page 5
    _back_by(lab, timedelta(hours=3))  # the evening before
    relay.read_in_kavita("mum", items[2], 5)  # in Kavita: chapter 3 opened, chapter 2 skipped
    relay.sync_all()
    assert progress_of(lab, digest)["progress"] == str(2 * PAGES + 5)  # what the GET gives
    put(lab, digest, 2 * PAGES + 5)  # KOReader goes there and, closed, sends it back
    relay.clock.advance(timedelta(minutes=10))
    relay.sync_all()
    assert relay.kavita_page("mum", items[1]) == 0  # Kavita is never told chapter 2 is read
    assert relay.unit_state("mum", items[1]) != "read"  # nobody turned a page of it


@pytest.mark.xfail(
    strict=True,
    reason="M1: the traversal of a block chains the positions of two e-readers: the anchor of "
    "one device and the page of another make a move nobody made",
)
def test_two_e_readers_never_make_one_move_that_pushes_a_chapter_read(lab: Any) -> None:
    relay, items, digest = three(lab)

    def put_from(device: str, at: int) -> None:
        answer = push(lab.mum, digest, str(at), floor4(at, 3 * PAGES), device_id=device)
        assert answer.status_code == 200, answer.text

    put_from("B", 1)
    put_from("B", 2 * PAGES + 5)  # the phone: a jump to chapter 3, at once (nothing crossed)
    _back_by(lab, timedelta(hours=3))
    put_from("A", 5)  # the e-reader: chapter 1, page 5
    _back_by(lab, timedelta(hours=1))
    put_from("B", 2 * PAGES + 6)  # the phone turns one page of chapter 3
    relay.sync_all()
    assert relay.kavita_page("mum", items[1]) == 0  # chapter 2: no device went through it
    assert relay.unit_state("mum", items[1]) != "read"


@pytest.mark.xfail(
    strict=True,
    reason="m1: the ceiling is read once, at the start of the pass, not before each chapter: a "
    "ceiling lowered during a pass still lets the chapters after it be pushed",
)
def test_a_ceiling_lowered_during_a_pass_stops_the_next_chapters(lab: Any) -> None:
    relay = build_relay(lab)
    items = [relay.add_chapter(Chapter.simple(str(n), count=PAGES)) for n in (1, 2, 3)]
    relay.link("kid")
    digest, _ = block(lab, lab.kid)
    for at in (5, 15, 25):
        put(lab, digest, at, reader=lab.kid)
    done: list[str] = []

    def lower_after_the_first_write(request: Any) -> None:
        if request.method == "POST" and "/reader/progress" in request.path.lower() and not done:
            done.append(request.path)
            with relay.engine.begin() as c:
                c.execute(sa.text("UPDATE user SET max_age_rating = 5 WHERE username = 'kid'"))

    relay.fake.hook = lower_after_the_first_write
    relay.sync_all()
    assert len(relay.writes("kid")) == 1  # the write in flight, none after the ceiling moved
    assert [relay.kavita_page("kid", i) for i in items].count(0) == 2
