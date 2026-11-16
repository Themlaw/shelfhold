"""M4 L50, #301 (contradicteur M2, m2, m3): what is told to Kavita is bounded in time.

A chapter a move crossed is pushed « read » only if the move started from a recent contact of the
same device: the delay since its previous position is at most `reading.relay_max_session_hours`
(24 h by default: KOReader makes no contact by itself unless its auto-sync is on). The local mark
is not bounded. A chapter whose file was replaced is no longer `verified`; the fake Kavita counts
the pages as Kavita does (images only).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from shelfhold.reading.application.sync import DEFAULT_MAX_SESSION_HOURS
from tests.bundle_env import Chapter
from tests.integration.test_l47_amendments import _back_by
from tests.integration.test_m4_l50_contradicteur import lab
from tests.integration.test_m4_l50_relay import PAGES, put, three
from tests.relay_env import _is_image

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014", "EF-EXP-026", "EF-USR-006")

__all__ = ["lab"]

DAY = timedelta(days=1)


def test_the_default_bound_is_a_day() -> None:
    assert DEFAULT_MAX_SESSION_HOURS == 24.0


def test_open_read_for_two_hours_and_close_pushes_the_chapters_read(lab):  # type: ignore[no-untyped-def]
    relay, items, digest = three(lab)
    put(lab, digest, 5)  # opened (or the last position sent before)
    _back_by(lab, timedelta(hours=2))
    put(lab, digest, 2 * PAGES + 5)  # two hours later, closed in chapter 3
    relay.clock.advance(timedelta(minutes=10))
    relay.sync_all()
    assert relay.unit_state("mum", items[1]) == "read"
    assert relay.kavita_page("mum", items[1]) > 0  # told to Kavita


def test_a_jump_after_days_without_contact_is_read_here_and_not_told_to_kavita(lab):  # type: ignore[no-untyped-def]
    relay, items, digest = three(lab)
    put(lab, digest, 1)  # Monday: page 1
    _back_by(lab, 3 * DAY)
    put(lab, digest, 2 * PAGES + 5)  # Thursday: chapter 3, nothing in between
    relay.clock.advance(timedelta(minutes=10))
    relay.sync_all()
    assert relay.unit_state("mum", items[1]) == "read"  # the local mark is not bounded (undoable)
    assert relay.kavita_page("mum", items[1]) == 0  # Kavita is not told


@pytest.mark.parametrize(
    ("delay", "told"),
    [
        (timedelta(hours=24), True),
        (timedelta(hours=24, seconds=1), False),
    ],
)
def test_the_bound_is_exact(lab, delay, told):  # type: ignore[no-untyped-def]
    relay, items, digest = three(lab)
    put(lab, digest, 1)
    _back_by(lab, delay)
    put(lab, digest, 2 * PAGES + 5)
    relay.clock.advance(timedelta(minutes=10))
    relay.sync_all()
    assert relay.unit_state("mum", items[1]) == "read"
    assert (relay.kavita_page("mum", items[1]) > 0) is told


def test_a_chapter_whose_file_was_replaced_is_no_longer_verified(lab):  # type: ignore[no-untyped-def]
    relay, items, _digest = three(lab)
    relay.sync_all()
    assert relay.states()[items[1].item_id] == "verified"
    relay.replace_chapter(items[1], Chapter.simple("2", count=PAGES, ext="jpg"))
    relay.sync_all()
    assert relay.states()[items[1].item_id] == "unverified"
    assert relay.states()[items[0].item_id] == "verified"


@pytest.mark.parametrize(
    ("name", "counted"),
    [
        ("001.png", True), ("a/b.JPG", True), ("c.jpeg", True), ("d.webp", True),
        ("e.gif", True), ("f.avif", True), ("ComicInfo.xml", False), (".hidden.png", False),
        ("__MACOSX/001.png", False), ("a/.001.png", False), ("notes.txt", False), ("png", False),
    ],
)  # fmt: skip
def test_the_fake_kavita_counts_only_images_like_kavita(name: str, counted: bool) -> None:
    assert _is_image(name) is counted
