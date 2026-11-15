"""M4 L48, contradicteur of PR #288: what the OPDS feed still lets through.

`allowed_profiles` (`shelf.allowed_profiles`, a parental-control action) is not read by the feed:
a profile that is not on the list of a shelf still sees its name and opens it. In the fixture of
`tests/visibility_env.py` the shelf "Family" is reserved to another profile (`someone-else`), and
both the child and the member see it. The two xfail(strict) tests turn green the day the feed
reads the list (D-P3, `shelf_open_to`).

The other tests pin what was tried and holds: an oracle through HEAD or OPTIONS, and a `Range`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from tests.integration.test_l48_opds import (
    ABSENT_ULID,
    World,
    make_world,
    parse,
    titles,
)

pytestmark = pytest.mark.req("EF-INT-007", "EF-USR-006", "ENF-SEC-010")

_ALLOWED_PROFILES = (
    "allowed_profiles is not read by the OPDS feed (#288, contradicteur): a shelf reserved "
    "to another profile is named and opened"
)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


def test_a_child_is_not_shown_a_shelf_reserved_to_another_profile(world: World) -> None:
    shelf = world.seed.shelves["family"]  # allowed_profiles == ["someone-else"]
    assert "Family" not in titles(parse(world.kid.get("/opds/shelves")))
    assert world.kid.get(f"/opds/shelves/{shelf}").status_code == 404


def test_a_member_is_not_shown_a_shelf_reserved_to_another_profile(world: World) -> None:
    shelf = world.seed.shelves["family"]
    assert "Family" not in titles(parse(world.mum.get("/opds/shelves")))
    assert world.mum.get(f"/opds/shelves/{shelf}").status_code == 404


def _answer(
    client: TestClient, method: str, path: str, headers: dict[str, str]
) -> tuple[int, bytes]:
    got = client.request(method, path, headers=headers)
    return got.status_code, got.content


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_head_and_options_cannot_tell_a_hidden_work_from_an_absent_one(
    world: World, method: str
) -> None:
    hidden = world.seed.works["above"]
    for headers in (world.kid.headers, {}):
        seen = _answer(world.kid.client, method, f"/opds/works/{hidden}", headers)
        absent = _answer(world.kid.client, method, f"/opds/works/{ABSENT_ULID}", headers)
        assert seen == absent
        assert seen[0] != 200


def test_a_range_on_a_cover_gets_the_whole_cover_or_nothing(world: World) -> None:
    digest = world.covers["ok"]
    whole = world.kid.get(f"/opds/covers/{digest}/160")
    ranged = world.kid.get(f"/opds/covers/{digest}/160", headers={"Range": "bytes=0-3"})
    assert ranged.status_code in {200, 404}
    if ranged.status_code == 200:
        assert ranged.content == whole.content
    hidden = world.kid.get(
        f"/opds/covers/{world.covers['above']}/160", headers={"Range": "bytes=0-3"}
    )
    assert hidden.status_code == 404
