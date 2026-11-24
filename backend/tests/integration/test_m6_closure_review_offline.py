"""Contradicteur D (M6 L20, D-P7 b): a child's offline copy of an Edition that leaves her view
while its Work stays visible through another Edition is never named by `?since=`.

The client (`frontend/src/lib/offline/sync.ts::syncSince`) evicts on a 200 by Work id only
(`store.evictWorks(profileId, answer.removed)`), then moves its token forward. The removals of
`?since=` are per Work (`catalog/application/changes.py::track_shelf_changes`): an Edition moved
to a shelf closed to the child, while another Edition of the same Work stays on an open shelf,
writes no event for her. Her copy of that Edition is therefore never evicted, even online, and
stays readable each time the device goes offline.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from tests.integration.test_l05a_content_route import World
from tests.integration.test_l05a_content_route import _world as _make_world
from tests.integration.test_m5_l06_publication import (
    ServerCutArchives,
    book,
    pages_of,
    publication,
)
from tests.integration.test_m6_l20_offline_profiles import removed, token_of


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _make_world(monkeypatch, settings)


@pytest.mark.req("EF-LEC-012", "EF-INT-001", "ENF-SEC-010")
def test_child_copy_of_edition_moved_off_her_shelves_is_named_by_since(world: World) -> None:
    content = book(world, pages_of(3), edition="ok")  # Work "ok" also has "ok_second", open to her
    kept = publication(world.kid, "content", content)
    assert kept.status_code == 200, kept.text  # she may keep it (D-P7 b)
    work = kept.json()["work_id"]
    token = token_of(world.kid)

    # the admin files that Edition on the shelf reserved to another profile (D-P3)
    moved = world.env.post(
        f"/api/v1/editions/{world.seed.editions['ok']}/shelf",
        json={"shelf_id": world.seed.shelves["family"]},
    )
    assert moved.status_code == 200 and moved.json()["filed"], moved.text

    # she may no longer read it: the server answers as for a thing that does not exist ...
    assert publication(world.kid, "content", content).status_code == 404
    # ... yet the Work is still hers (another Edition carries it)
    assert publication(world.kid, "unit", "0" * 26).status_code == 404  # sanity of the helper

    got = removed(world.kid, token)
    assert got.status_code == 200, got.text
    # Decision of the closure review (S3): the Work is still hers, so it is not in `removed`; the
    # Edition she lost is named by `removed_editions`, and the client evicts the copy by edition.
    assert got.json()["removed"] == []
    edition = world.seed.editions["ok"]
    assert edition in got.json()["removed_editions"], (
        "the child's offline copy of an Edition she may no longer see is never evicted: "
        f"?since= answered 200 with removed_editions={got.json().get('removed_editions')!r}"
    )
    assert work not in got.json()["removed"]
    assert kept.json()["edition_id"] == edition  # the copy knows its Edition
