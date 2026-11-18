"""M5 L07, relecture adverse indépendante (#369): the holes found, each a red proof in strict xfail.

Each test states what the member's data should be and fails on the branch as delivered; the fix
turns it green and `strict=True` then asks for the marker to go. See `docs/decisions/M5.md`,
section « #369 : contradicteur ».
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export, reading
from shelfhold.reading.public import progress_of
from tests.auth_env import CSRF_HEADER
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import count, current, history, kept, page, put


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


@pytest.mark.req("EF-LEC-007")
@pytest.mark.xfail(
    strict=True,
    reason="C1: the winner is taken among the last HISTORY_LIMIT rows by server_ts, not by "
    "bounded client_ts: a batch of late offline positions pushes the latest one out of the window",
)
def test_late_offline_batch_never_moves_the_reader_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latest position (by bounded `client_ts`) stays the answer however many older positions
    arrive after it. The window is shrunk to 3 so that the test does not write 1 000 rows; with the
    real `HISTORY_LIMIT` a phone that queued 1 000 page turns offline does the same."""
    monkeypatch.setattr("shelfhold.reading.application.positions.HISTORY_LIMIT", 3)
    content, _unit = kept(world)
    latest = put(world, world.admin, content, page(25), device="tab", minutes=-1)
    assert latest.status_code == 200, latest.text
    for minutes, index in ((-120, 1), (-119, 2), (-118, 3)):  # read offline two hours ago
        assert put(world, world.admin, content, page(index), device="phone", minutes=minutes)
    got = current(world.admin, content).json()["position"]
    assert got["id"] == latest.json()["position"]["id"]  # still where the tablet left her
    listed = history(world.admin, content).json()["items"]
    assert listed[0]["id"] == latest.json()["position"]["id"]  # « the latest first »


@pytest.mark.req("EF-LEC-007")
@pytest.mark.xfail(
    strict=True,
    reason="C2: an open proposal is never revisited: once the far device writes again, its "
    "« winner » is no longer where the reader is, and choosing « loser » moves her back",
)
def test_stale_proposal_never_moves_the_reader_back(world: World) -> None:
    content, _unit = kept(world)
    assert put(world, world.admin, content, page(20, 50), device="tab", minutes=-30).is_success
    proposal = put(world, world.admin, content, page(2, 50), device="phone", minutes=-10).json()
    assert proposal["conflict"] is not None  # « page 3 or page 21? »
    on = put(world, world.admin, content, page(40, 50), device="tab", minutes=-5)  # reads on
    assert on.json()["position"]["locator"]["index"] == 40
    shown = current(world.admin, content).json()
    # the API says `winner` is « the latest: where the reader is »; it is not any more
    if shown["conflict"] is not None:
        assert shown["conflict"]["winner"]["id"] == shown["position"]["id"]
    done = world.admin.post(
        f"/api/reader/conflicts/{proposal['conflict']['id']}/resolve",
        json={"choice": "loser", "device_id": "phone"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    if done.status_code == 200:
        assert done.json()["position"]["locator"]["index"] >= 40  # never back to page 21


@pytest.mark.req("EF-LEC-014", "EF-LEC-007")
@pytest.mark.xfail(
    strict=True,
    reason="C3: the reader stores `page = index` (from 0) while the coverage counts pages from 1: "
    "turning every page of a Unit in the reader never reaches its last page, never marks it read",
)
def test_reading_every_page_in_the_reader_marks_the_unit_read(world: World) -> None:
    content, unit = kept(world)
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(export.unit_coverage)
            .where(export.unit_coverage.c.carrier_id == content)
            .values(start_locator={"page": 1}, end_locator={"page": 30})
        )  # fmt: skip
    for index in range(30):  # page 1 to page 30, one every 30 seconds: a plausible pass
        world.env.clock.advance(timedelta(seconds=30))
        assert put(world, world.admin, content, page(index), device="tab").status_code == 200
    admin = user_id_of(world.seed.engine, "admin")
    with world.seed.engine.connect() as connection:
        (shown,) = progress_of(connection, viewer_id=admin, subject_id=admin, unit_ids=[unit])
    assert shown.state == "read"


@pytest.mark.req("EF-LEC-007")
@pytest.mark.xfail(
    strict=True,
    reason="C4: an aware `client_ts` near the bounds of datetime passes the 5 minute check and "
    "overflows when stored in UTC: an unhandled 500 instead of a 422",
)
def test_extreme_client_ts_is_a_422_not_a_500(world: World) -> None:
    content, _unit = kept(world)
    got = world.admin.put(
        "/api/reader/positions",
        json={"carrier_type": "content", "carrier_id": content, "locator": page(3),
              "client_ts": "0001-01-01T00:00:00+14:00", "device_id": "tab"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )  # fmt: skip
    assert got.status_code == 422, got.text
    assert count(world, reading.reading_position) == 0
