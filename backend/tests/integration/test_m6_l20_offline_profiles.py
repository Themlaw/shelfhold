"""M6 L20 (EF-LEC-012, points 5 and 6 of M5, D-P7 (b)): the offline copy belongs to every profile.

« Lire hors ligne » lives in the reader, which every profile opens — the sheet of a series is the
administrator's, so the member and the child had no screen to keep a copy from. The server side of
a copy is the publication a copy is made of: a member and a child each get the full one, named by
the Work `?since=` evicts by, with entries their own session may fetch; what a child may not read
answers as a thing that does not exist. Two guards of the eviction: the reclassification of M5
L16b (the child's copy of a Work raised above her ceiling is named in her removals, and the carrier
answers 404 to her), and a ceiling lowered for the child (the token's scope changed: a
resynchronisation, then the copy asked again answers 404, so the client evicts it). Real
application, real sessions.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _shape
from tests.integration.test_l05a_content_route import _world as _make_world
from tests.integration.test_m5_l06_publication import (
    ServerCutArchives,
    book,
    fetch,
    pages_of,
    publication,
    unit_of,
)

WORKS = "/api/v1/works"
ABSENT = "0" * 26


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _make_world(monkeypatch, settings)


def rate(world: World, key: str, age: str) -> None:
    work = world.seed.works[key]
    assert world.env.post(f"{WORKS}/{work}/rating", json={"age": age}).status_code == 200


def token_of(client: Any) -> str:
    answer = client.get(WORKS, params={"limit": 200})
    assert answer.status_code == 200, answer.text
    return str(answer.json()["since"])


def removed(client: Any, token: str) -> Any:
    return client.get(WORKS, params={"since": token, "limit": 200})


@pytest.mark.req("EF-LEC-012", "EF-USR-006", "ENF-SEC-010")
def test_offline_copy_available_to_member_and_child(world: World) -> None:
    """Every profile that opens the reader may keep a copy (D-P7 (b)): a member and a child each
    get the full publication of the very pair the reader route takes (`unit`) and of the copy the
    server chose (`content`), every entry a link their own session may fetch. A Work above the
    child's ceiling is not offered to her: the same answer as for a carrier that does not exist."""
    content = book(world, pages_of(3))
    unit = unit_of(world, content)
    for client in (world.mum, world.kid):
        for carrier_type, carrier_id in (("unit", unit), ("content", content)):
            got = publication(client, carrier_type, carrier_id)
            assert got.status_code == 200, (carrier_type, got.text)
            manifest = got.json()
            assert manifest["scope"] == "full"
            assert manifest["work_id"] == world.seed.works["ok"]
            for entry in [*manifest["pages"], *manifest["spine"]]:
                served = fetch(world, client, entry["href"])
                assert served.status_code == 200, entry["href"]
    hidden = book(world, pages_of(3), edition="twelve")
    absent: Any = publication(world.kid, "content", ABSENT)
    assert _shape(publication(world.kid, "content", hidden)) == _shape(absent)
    assert "work_id" not in publication(world.kid, "content", hidden).text
    assert publication(world.kid, "unit", unit_of(world, hidden)).status_code == 404
    # the member reads it: her copy is not the child's refusal
    assert publication(world.mum, "content", hidden).status_code == 200


@pytest.mark.req("EF-LEC-012", "EF-INT-001", "EF-USR-006", "ENF-SEC-010")
def test_offline_copy_evicted_after_reclassification(world: World) -> None:
    """Unchanged since M5 L16b: the copy of a Work the admin then rates above the child's ceiling
    is named in her removals, and the Work answers her publication as a thing that does not
    exist. The member's own token names nothing of it: her copy is not evicted."""
    content = book(world, pages_of(3))
    unit = unit_of(world, content)
    work = publication(world.kid, "unit", unit).json()["work_id"]
    token = token_of(world.kid)
    hers = token_of(world.mum)  # taken while she still saw the Work: it must not name it after
    rate(world, "ok", "18+")
    got = removed(world.kid, token)
    assert got.status_code == 200 and work in got.json()["removed"]
    for carrier in (("content", content), ("unit", unit)):
        assert publication(world.kid, *carrier).status_code == 404
    assert work not in removed(world.mum, hers).json()["removed"]
    assert publication(world.mum, "content", content).status_code == 200


@pytest.mark.req("EF-LEC-012", "EF-INT-001", "EF-USR-006", "ENF-SEC-010")
def test_a_ceiling_lowered_for_a_child_is_a_resynchronisation_and_a_404(world: World) -> None:
    """The ceiling is not in the token's scope for nothing (M3 L11): a child's ceiling lowered under
    a Work she holds a copy of answers her token with a resynchronisation, and the copy asked again
    answers 404 — the client evicts it. The member's copy is not the child's refusal."""
    content = book(world, pages_of(3))
    unit = unit_of(world, content)
    assert publication(world.kid, "unit", unit).status_code == 200  # she may keep it
    token = token_of(world.kid)
    hers = token_of(world.mum)

    kid = user_id_of(world.seed.engine, "kid")
    world.env.app.state.auth_gate.auth.profile_set(kid, max_age_rating=0)  # type: ignore[attr-defined]

    got = removed(world.kid, token)
    assert got.status_code == 410, got.text  # her scope changed: ask every copy again
    for carrier in (("content", content), ("unit", unit)):
        assert publication(world.kid, *carrier).status_code == 404  # what she may no longer read
    absent: Any = publication(world.kid, "content", ABSENT)
    # the same shape as for a carrier that does not exist: no oracle in the refusal
    assert _shape(publication(world.kid, "content", content)) == _shape(absent)
    # the member's token still stands, her publication still serves
    assert removed(world.mum, hers).status_code == 200
    assert publication(world.mum, "content", content).status_code == 200
