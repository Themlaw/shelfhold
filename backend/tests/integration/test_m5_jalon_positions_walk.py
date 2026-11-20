"""J5-POS-1 (milestone review of M5): what still marks a Unit read once the web reader is judged
by pages turned, not by speed. The red cases are in `test_m5_jalon_positions.py`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest

from shelfhold.config import Settings
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_jalon_positions import _paged, _state
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import page, put


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


@pytest.mark.req("EF-LEC-007", "EF-LEC-014")
def test_web_reader_turning_page_after_page_to_the_end_marks_read(world: World) -> None:
    """The reader writes as pages are turned: a walk of small steps (a page at a time, the kept copy
    carries no page bounds, so it is judged by share) covers the Unit, at any pace."""
    content, unit = _paged(world)
    for index in range(30):
        assert put(world, world.admin, content, page(index)).status_code == 200
        world.env.clock.advance(timedelta(hours=3))  # a slow reader: the pace is not the proof
    assert _state(world, unit) == "read"


@pytest.mark.req("EF-LEC-007", "EF-LEC-014")
def test_web_reader_jump_after_a_few_pages_marks_nothing(world: World) -> None:
    """Three small steps, then a jump to the last page a day later: the pages between were not
    turned, however slow the jump."""
    content, unit = _paged(world)
    for index in (0, 2, 4):
        assert put(world, world.admin, content, page(index)).status_code == 200
        world.env.clock.advance(timedelta(minutes=1))
    world.env.clock.advance(timedelta(days=1))
    assert put(world, world.admin, content, page(29)).status_code == 200
    assert _state(world, unit) == "in_progress"
