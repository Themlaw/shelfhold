"""J5-POS-1 (milestone review of M5), the other side: the rule by speed stays KOReader's.

Through the real kosync service, a book read at human speed with rare pushes is marked read, on
the two paths that judge a pass by pages: a paged file (`locator = {"page": …}`) and the block of a
series (`locator["bundle"]`). The jump that is a look stays a look. The device of KOReader says its
own `device_id` here, as in production: what decides the rule is the channel, not that word.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from tests.bundle_env import Chapter
from tests.integration.test_l47_amendments import _back_by, floor4
from tests.integration.test_l47_kosync import block, push, volume
from tests.integration.test_l48_opds import World, make_world
from tests.relay_env import build_relay


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


def _state(lab: World, unit: str) -> str:
    with lab.seed.engine.connect() as c:
        found = (
            c.execute(sa.text("SELECT state FROM reading_progress WHERE unit_id = :u"), {"u": unit})
            .scalars()
            .all()
        )
    return found[0] if found else "unread"


@pytest.mark.req("EF-LEC-009", "EF-LEC-007")
def test_a_paged_file_closed_far_apart_by_kosync_at_a_human_speed_is_marked_read(
    lab: World,
) -> None:
    """KOReader pushes when the book is closed: page 1, then page 30 an hour later (the door keeps
    the first and the last position of a window, the speed of the move is what counts)."""
    item, digest = volume(lab, count=30)
    assert push(lab.mum, digest, "1", 1 / 30).status_code == 200
    _back_by(lab, timedelta(hours=1))
    assert push(lab.mum, digest, "30", 1.0).status_code == 200
    assert _state(lab, item.unit_id) == "read"


@pytest.mark.req("EF-LEC-009", "EF-LEC-007")
def test_a_paged_file_jumped_to_the_end_by_kosync_is_not_read(lab: World) -> None:
    item, digest = volume(lab, count=30)
    assert push(lab.mum, digest, "1", 1 / 30).status_code == 200
    assert push(lab.mum, digest, "30", 1.0).status_code == 200  # the same second: a look
    assert _state(lab, item.unit_id) != "read"


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_a_block_read_through_by_kosync_marks_the_chapters(lab: World) -> None:
    pages = 10
    relay = build_relay(lab)
    items = [relay.add_chapter(Chapter.simple(str(n), count=pages)) for n in (1, 2, 3)]
    relay.link("mum")
    digest, _ = block(lab, lab.mum)
    total = 3 * pages
    assert push(lab.mum, digest, "1", floor4(1, total)).status_code == 200
    _back_by(lab, timedelta(hours=1))
    assert push(lab.mum, digest, "25", floor4(25, total)).status_code == 200
    assert [_state(lab, i.unit_id) for i in items] == ["read", "read", "in_progress"]
