"""PR #396 (M5 L16b), contradicteur: a copy that outlives the view of a child.

`?since=` speaks of Works, the offline copies are keyed by the Work their publication named. One
Edition of a Work rated above the child's ceiling alone (`POST /editions/{id}/rating`, EF-USR-006):
the Work stays in her view through its other Edition. Checked, no hole: the reconnection still tells
the client (the Work in the removals, or a resynchronisation, 410), so the copy is asked again and
answers 404. Kept as a guard: a change that let the Edition go unseen would leave the copy readable.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from tests.integration.test_l05a_content_route import World
from tests.integration.test_l05a_content_route import _world as _make_world
from tests.integration.test_m5_l06_publication import ServerCutArchives, book, pages_of, publication
from tests.integration.test_m5_l16b_offline import removed, token_of


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _make_world(monkeypatch, settings)


@pytest.mark.req("EF-LEC-012", "EF-INT-001", "EF-USR-006")
def test_copy_of_an_edition_rated_above_the_ceiling_alone_is_evicted(world: World) -> None:
    content = book(world, pages_of(3), edition="ok_second")
    kept = publication(world.kid, "content", content)
    assert kept.status_code == 200, kept.text  # the child may keep it: the copy is made
    work = kept.json()["work_id"]
    token = token_of(world.kid)  # the token the client holds with its copy
    edition = world.seed.editions["ok_second"]
    rated = world.env.post(f"/api/v1/editions/{edition}/rating", json={"age": "18+"})
    assert rated.status_code == 200, rated.text
    # the child may no longer read it online ...
    assert publication(world.kid, "content", content).status_code == 404
    # ... and the reconnection must say so, else the copy stays readable offline for good
    got = removed(world.kid, token)
    assert got.status_code == 410 or work in got.json()["removed"], got.text
